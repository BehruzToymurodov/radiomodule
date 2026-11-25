/*
 * link_proto.h - HM30 link packet format (no HAL, plain C99, testable on a PC).
 *
 * Frame: 0xAA 0x55 | type | seq | len | payload[len] | crc_lo crc_hi
 * CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) over type, seq, len, payload.
 * Must match tools/hm30_link/pc/protocol.py.
 */
#ifndef LINK_PROTO_H
#define LINK_PROTO_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define LINK_SYNC1        0xAAu
#define LINK_SYNC2        0x55u
#define LINK_MAX_PAYLOAD  240u
#define LINK_OVERHEAD     7u    /* 2 sync + type + seq + len + 2 crc */
#define LINK_MAX_FRAME    (LINK_MAX_PAYLOAD + LINK_OVERHEAD)

#define LINK_TYPE_TEXT    0x01u /* UTF-8 text                          */
#define LINK_TYPE_VALUE   0x02u /* float32 LE + name (UTF-8)           */
#define LINK_TYPE_BYTES   0x03u /* raw bytes                           */
#define LINK_TYPE_CMD     0x04u /* UTF-8 command, PC -> STM32          */
#define LINK_TYPE_ACK     0x05u /* STM32 -> PC: [seq, type] of a packet it accepted */
#define LINK_TYPE_HELLO   0x06u /* PC -> STM32 on connect: resets the duplicate filter */

uint16_t link_crc16(const uint8_t *data, size_t len);

/* Builds a frame into out (size >= LINK_MAX_FRAME). Returns frame length, 0 if payload too long. */
size_t link_build_frame(uint8_t *out, uint8_t type, uint8_t seq,
                        const uint8_t *payload, size_t len);

/* Byte-by-byte receive parser. */
typedef struct {
    uint8_t  state;
    uint8_t  type;
    uint8_t  seq;
    uint8_t  len;
    uint8_t  pos;
    uint8_t  crc_lo;
    uint8_t  payload[LINK_MAX_PAYLOAD + 1]; /* +1 so text payloads can be 0-terminated */
    uint32_t crc_errors;
} link_parser_t;

void link_parser_init(link_parser_t *p);

/* Feed one byte. Returns 1 when a complete, CRC-valid frame is ready in
 * p->type / p->seq / p->len / p->payload (payload[len] is set to 0). */
int link_parser_feed(link_parser_t *p, uint8_t b);

#ifdef __cplusplus
}
#endif

#endif /* LINK_PROTO_H */
