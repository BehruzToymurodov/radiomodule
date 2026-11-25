/*
 * link.c - HM30 datalink for STM32 HAL. See link.h.
 *
 * Transmit: frames go into a ring buffer and are sent with HAL_UART_Transmit_IT, so
 *           link_send_* returns immediately and the RX interrupt can queue ACKs.
 * Receive:  1 byte per interrupt -> parser -> queue of whole frames for link_receive().
 *           Every accepted PC packet is answered with an ACK at once (from the interrupt).
 *           Re-sent packets (ACK lost on the way back) are ACKed again but not queued twice.
 */
#include "link.h"
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#if (LINK_TX_BUF & (LINK_TX_BUF - 1u)) != 0u
#error "LINK_TX_BUF must be a power of two"
#endif

static UART_HandleTypeDef *s_uart;
static volatile uint8_t s_seq;

/* ---- transmit ring buffer ------------------------------------------------- */
static uint8_t           s_tx[LINK_TX_BUF];
static volatile uint16_t s_tx_head;      /* next byte to write          */
static volatile uint16_t s_tx_tail;      /* next byte to send           */
static volatile uint16_t s_tx_inflight;  /* bytes handed to the HAL now */
static volatile uint32_t s_tx_dropped;

/* ---- receive ---------------------------------------------------------------- */
static uint8_t       s_rx_byte;
static link_parser_t s_parser;
static link_frame_t  s_queue[LINK_RX_QUEUE];
static volatile uint8_t  s_q_head;       /* written by the UART interrupt */
static volatile uint8_t  s_q_tail;       /* written by the main loop      */
static volatile uint32_t s_rx_dropped;
static volatile uint32_t s_rx_duplicates;
static uint8_t s_seen[32];               /* 256-bit map of recently accepted PC seq numbers */

static uint16_t tx_free(void)
{
    return (uint16_t)(LINK_TX_BUF - 1u - ((s_tx_head - s_tx_tail) & (LINK_TX_BUF - 1u)));
}

/* Start sending the next contiguous block if the UART is idle. Interrupts must be off
 * (or we are in the UART interrupt). */
static void tx_kick(void)
{
    if (s_tx_inflight || s_tx_head == s_tx_tail)
        return;
    uint16_t tail = s_tx_tail;
    uint16_t len = (s_tx_head > tail) ? (uint16_t)(s_tx_head - tail) : (uint16_t)(LINK_TX_BUF - tail);
    s_tx_inflight = len;
    if (HAL_UART_Transmit_IT(s_uart, &s_tx[tail], len) != HAL_OK)
        s_tx_inflight = 0;               /* try again on the next send */
}

/* Copies a whole frame into the TX buffer. from_isr: never wait, drop if full. */
static int tx_push_frame(uint8_t type, const uint8_t *payload, size_t len, int from_isr)
{
    uint8_t frame[LINK_MAX_FRAME];
    uint32_t start = HAL_GetTick();
    for (;;) {
        uint32_t primask = __get_PRIMASK();
        __disable_irq();
        size_t n = link_build_frame(frame, type, s_seq, payload, len);
        if (n && tx_free() >= n) {
            s_seq++;
            uint16_t head = s_tx_head;
            for (size_t i = 0; i < n; i++) {
                s_tx[head] = frame[i];
                head = (uint16_t)((head + 1u) & (LINK_TX_BUF - 1u));
            }
            s_tx_head = head;
            tx_kick();
            __set_PRIMASK(primask);
            return 1;
        }
        __set_PRIMASK(primask);
        if (!n || from_isr || HAL_GetTick() - start > LINK_TX_TIMEOUT_MS) {
            s_tx_dropped++;
            return 0;
        }
        /* main loop: wait for the interrupt to make room */
    }
}

static void send_frame(uint8_t type, const uint8_t *payload, size_t len)
{
    if (!s_uart)
        return;
    if (len > LINK_MAX_PAYLOAD)
        len = LINK_MAX_PAYLOAD;
    tx_push_frame(type, payload, len, 0);
}

void link_init(UART_HandleTypeDef *huart)
{
    s_uart = huart;
    s_seq = 0;
    s_tx_head = s_tx_tail = s_tx_inflight = 0;
    s_q_head = s_q_tail = 0;
    s_tx_dropped = s_rx_dropped = s_rx_duplicates = 0;
    memset(s_seen, 0, sizeof s_seen);
    link_parser_init(&s_parser);
    HAL_UART_Receive_IT(s_uart, &s_rx_byte, 1);
}

void link_send_text(const char *text)
{
    send_frame(LINK_TYPE_TEXT, (const uint8_t *)text, strlen(text));
}

void link_send_value(const char *name, float value)
{
    uint8_t buf[LINK_MAX_PAYLOAD];
    size_t name_len = strlen(name);
    if (name_len > LINK_MAX_PAYLOAD - 4u)
        name_len = LINK_MAX_PAYLOAD - 4u;
    memcpy(buf, &value, 4);           /* Cortex-M is little-endian, IEEE-754 float */
    memcpy(buf + 4, name, name_len);
    send_frame(LINK_TYPE_VALUE, buf, 4u + name_len);
}

void link_send_bytes(const uint8_t *data, size_t len)
{
    send_frame(LINK_TYPE_BYTES, data, len);
}

void link_printf(const char *fmt, ...)
{
    char buf[LINK_MAX_PAYLOAD + 1];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    if (n < 0)
        return;
    if ((size_t)n > LINK_MAX_PAYLOAD)
        n = LINK_MAX_PAYLOAD;
    send_frame(LINK_TYPE_TEXT, (const uint8_t *)buf, (size_t)n);
}

int link_receive(link_frame_t *out)
{
    uint8_t tail = s_q_tail;
    if (tail == s_q_head)
        return 0;
    const link_frame_t *f = &s_queue[tail];
    out->type = f->type;
    out->seq = f->seq;
    out->len = f->len;
    memcpy(out->data, f->data, (size_t)f->len + 1u);   /* includes the 0 */
    s_q_tail = (uint8_t)((tail + 1u) % LINK_RX_QUEUE);  /* free the slot only after copying */
    return 1;
}

int link_frame_value(const link_frame_t *f, const char **name, float *value)
{
    if (f->type != LINK_TYPE_VALUE || f->len < 4u)
        return 0;
    memcpy(value, f->data, 4);
    *name = (const char *)&f->data[4];                 /* 0-terminated by data[len] = 0 */
    return 1;
}

size_t link_get_command(char *out, size_t out_size)
{
    link_frame_t f;
    if (out_size == 0)
        return 0;
    while (link_receive(&f)) {
        if (f.type == LINK_TYPE_CMD) {
            strncpy(out, (const char *)f.data, out_size - 1);
            out[out_size - 1] = '\0';
            return strlen(out);
        }
    }
    return 0;
}

uint32_t link_rx_crc_errors(void) { return s_parser.crc_errors; }
uint32_t link_rx_dropped(void)    { return s_rx_dropped; }
uint32_t link_rx_duplicates(void) { return s_rx_duplicates; }
uint32_t link_tx_dropped(void)    { return s_tx_dropped; }

static int seen(uint8_t seq)  { return (s_seen[seq >> 3] >> (seq & 7u)) & 1u; }
static void mark(uint8_t seq)
{
    s_seen[seq >> 3] |= (uint8_t)(1u << (seq & 7u));
    uint8_t old = (uint8_t)(seq + 128u);               /* forget numbers half a wrap away */
    s_seen[old >> 3] &= (uint8_t)~(1u << (old & 7u));
}

static void handle_frame(void)
{
    uint8_t type = s_parser.type, seq = s_parser.seq;
    uint8_t ack[2] = { seq, type };

    if (type == LINK_TYPE_HELLO) {                     /* PC (re)connected */
        memset(s_seen, 0, sizeof s_seen);
        tx_push_frame(LINK_TYPE_ACK, ack, 2, 1);
        return;
    }
    if (type == LINK_TYPE_ACK)
        return;
    if (seen(seq)) {                                   /* PC re-sent it: our ACK got lost */
        s_rx_duplicates++;
        tx_push_frame(LINK_TYPE_ACK, ack, 2, 1);
        return;
    }
    uint8_t head = s_q_head;
    uint8_t next = (uint8_t)((head + 1u) % LINK_RX_QUEUE);
    if (next == s_q_tail) {                            /* queue full: no ACK, PC will retry */
        s_rx_dropped++;
        return;
    }
    link_frame_t *f = &s_queue[head];
    f->type = type;
    f->seq = seq;
    f->len = s_parser.len;
    memcpy(f->data, s_parser.payload, (size_t)s_parser.len + 1u);
    s_q_head = next;
    mark(seq);
    tx_push_frame(LINK_TYPE_ACK, ack, 2, 1);
}

void link_uart_rx_cplt(UART_HandleTypeDef *huart)
{
    if (huart != s_uart)
        return;
    if (link_parser_feed(&s_parser, s_rx_byte))
        handle_frame();
    HAL_UART_Receive_IT(s_uart, &s_rx_byte, 1);
}

void link_uart_tx_cplt(UART_HandleTypeDef *huart)
{
    if (huart != s_uart)
        return;
    s_tx_tail = (uint16_t)((s_tx_tail + s_tx_inflight) & (LINK_TX_BUF - 1u));
    s_tx_inflight = 0;
    tx_kick();
}

void link_uart_error(UART_HandleTypeDef *huart)
{
    if (huart != s_uart)
        return;
    /* Overrun / noise / framing error: clear it and keep receiving. */
    __HAL_UART_CLEAR_OREFLAG(huart);
    link_parser_init(&s_parser);
    HAL_UART_Receive_IT(s_uart, &s_rx_byte, 1);
}

#ifndef LINK_NO_HAL_CALLBACKS
void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart) { link_uart_rx_cplt(huart); }
void HAL_UART_TxCpltCallback(UART_HandleTypeDef *huart) { link_uart_tx_cplt(huart); }
void HAL_UART_ErrorCallback(UART_HandleTypeDef *huart)  { link_uart_error(huart); }
#endif
