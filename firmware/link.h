/*
 * link.h - send data to the PC app over the SIYI HM30 datalink (STM32 HAL, UART).
 *
 * Usage (main.c, after MX_USART1_UART_Init()):
 *
 *     link_init(&huart1);
 *     link_send_text("robot started");
 *     link_send_value("temp", 24.5f);
 *
 *     link_frame_t f;
 *     while (link_receive(&f)) { ... see "Receiving from the PC" below ... }
 *
 * Needs "USART1 global interrupt" enabled in CubeMX (NVIC Settings). Sending and
 * receiving both run on that interrupt. This file defines HAL_UART_RxCpltCallback,
 * HAL_UART_TxCpltCallback and HAL_UART_ErrorCallback; if your project already has them,
 * build with LINK_NO_HAL_CALLBACKS defined and call link_uart_rx_cplt() /
 * link_uart_tx_cplt() / link_uart_error() from yours.
 *
 * Reliability: every packet the PC app sends is confirmed by an ACK that this library
 * sends automatically from the interrupt. The app re-sends anything not confirmed, and
 * this library ignores the copies, so your code sees each packet exactly once.
 */
#ifndef LINK_H
#define LINK_H

#include "main.h"        /* CubeMX: pulls in the HAL for your STM32 family */
#include "link_proto.h"

#ifdef __cplusplus
extern "C" {
#endif

#ifndef LINK_TX_BUF
#define LINK_TX_BUF 1024u          /* transmit buffer, bytes (power of two)          */
#endif
#ifndef LINK_TX_TIMEOUT_MS
#define LINK_TX_TIMEOUT_MS 100u    /* max wait when the transmit buffer is full      */
#endif
#ifndef LINK_RX_QUEUE
#define LINK_RX_QUEUE 8u           /* received packets waiting for link_receive()   */
#endif

void link_init(UART_HandleTypeDef *huart);

/* Send functions copy the packet into the transmit buffer and return at once; the UART
 * interrupt sends it. They only wait if the buffer is full (then up to LINK_TX_TIMEOUT_MS,
 * after which the packet is dropped and counted in link_tx_dropped()).
 * Call them from the main loop, not from interrupts. */
void link_send_text(const char *text);
void link_send_value(const char *name, float value);
void link_send_bytes(const uint8_t *data, size_t len);
void link_printf(const char *fmt, ...);   /* formatted text, max LINK_MAX_PAYLOAD chars */

/* ---- Receiving from the PC ---------------------------------------------------
 * Every packet the PC sends (command, text, number, bytes, file chunk) goes into a
 * small queue (LINK_RX_QUEUE packets). Read them in your main loop:
 *
 *     link_frame_t f;
 *     while (link_receive(&f)) {
 *         switch (f.type) {
 *         case LINK_TYPE_CMD:   ... (char *)f.data ...            break;
 *         case LINK_TYPE_TEXT:  ... (char *)f.data ...            break;
 *         case LINK_TYPE_VALUE: { const char *name; float v;
 *                                 link_frame_value(&f, &name, &v); ... } break;
 *         case LINK_TYPE_BYTES: ... f.data[0 .. f.len-1] ...      break;
 *         }
 *     }
 *
 * If the queue is full (main loop too slow), the packet is not ACKed, so the PC app
 * re-sends it a little later; it is counted in link_rx_dropped().
 */
typedef struct {
    uint8_t type;                  /* LINK_TYPE_*                                  */
    uint8_t seq;
    uint8_t len;                   /* payload length                               */
    uint8_t data[LINK_MAX_PAYLOAD + 1]; /* payload, data[len] = 0 so text is a C string */
} link_frame_t;

/* Takes the oldest received packet out of the queue. Returns 1 if one was copied to out. */
int link_receive(link_frame_t *out);

/* For LINK_TYPE_VALUE packets: points *name at the (0-terminated) name and sets *value.
 * Returns 0 if f is not a valid VALUE packet. */
int link_frame_value(const link_frame_t *f, const char **name, float *value);

/* Simple alternative when you only need commands: returns the next CMD packet's text.
 * Note: packets of other types that are ahead of it in the queue are discarded.
 * Returns the text length, or 0 if no command is waiting. */
size_t link_get_command(char *out, size_t out_size);

/* Statistics */
uint32_t link_rx_crc_errors(void);  /* damaged packets from the PC                       */
uint32_t link_rx_dropped(void);     /* packets refused because the queue was full        */
uint32_t link_rx_duplicates(void);  /* re-sent copies that were ignored (ACK was lost)    */
uint32_t link_tx_dropped(void);     /* packets not sent because the TX buffer stayed full */

/* Call these from your own HAL callbacks if you define LINK_NO_HAL_CALLBACKS. */
void link_uart_rx_cplt(UART_HandleTypeDef *huart);
void link_uart_tx_cplt(UART_HandleTypeDef *huart);
void link_uart_error(UART_HandleTypeDef *huart);

#ifdef __cplusplus
}
#endif

#endif /* LINK_H */
