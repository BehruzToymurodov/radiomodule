/*
 * link_proto.c - HM30 link packet format (no HAL). See link_proto.h.
 */
#include "link_proto.h"
#include <string.h>

enum { S_SYNC1, S_SYNC2, S_TYPE, S_SEQ, S_LEN, S_PAYLOAD, S_CRC_LO, S_CRC_HI };

static uint16_t crc16_update(uint16_t crc, uint8_t b)
{
    crc ^= (uint16_t)b << 8;
    for (int i = 0; i < 8; i++)
        crc = (crc & 0x8000u) ? (uint16_t)((crc << 1) ^ 0x1021u) : (uint16_t)(crc << 1);
    return crc;
}

uint16_t link_crc16(const uint8_t *data, size_t len)
{
    uint16_t crc = 0xFFFFu;
    while (len--)
        crc = crc16_update(crc, *data++);
    return crc;
}

size_t link_build_frame(uint8_t *out, uint8_t type, uint8_t seq,
                        const uint8_t *payload, size_t len)
{
    if (len > LINK_MAX_PAYLOAD)
        return 0;
    out[0] = LINK_SYNC1;
    out[1] = LINK_SYNC2;
    out[2] = type;
    out[3] = seq;
    out[4] = (uint8_t)len;
    if (len)
        memcpy(&out[5], payload, len);
    uint16_t crc = link_crc16(&out[2], len + 3u);
    out[5 + len] = (uint8_t)(crc & 0xFFu);
    out[6 + len] = (uint8_t)(crc >> 8);
    return len + LINK_OVERHEAD;
}

void link_parser_init(link_parser_t *p)
{
    memset(p, 0, sizeof(*p));
    p->state = S_SYNC1;
}

int link_parser_feed(link_parser_t *p, uint8_t b)
{
    switch (p->state) {
    case S_SYNC1:
        if (b == LINK_SYNC1) p->state = S_SYNC2;
        break;
    case S_SYNC2:
        if (b == LINK_SYNC2)      p->state = S_TYPE;
        else if (b != LINK_SYNC1) p->state = S_SYNC1;
        break;
    case S_TYPE:
        p->type = b;
        p->state = S_SEQ;
        break;
    case S_SEQ:
        p->seq = b;
        p->state = S_LEN;
        break;
    case S_LEN:
        if (b > LINK_MAX_PAYLOAD) {
            p->crc_errors++;
            p->state = S_SYNC1;
            break;
        }
        p->len = b;
        p->pos = 0;
        p->state = b ? S_PAYLOAD : S_CRC_LO;
        break;
    case S_PAYLOAD:
        p->payload[p->pos++] = b;
        if (p->pos >= p->len) p->state = S_CRC_LO;
        break;
    case S_CRC_LO:
        p->crc_lo = b;
        p->state = S_CRC_HI;
        break;
    case S_CRC_HI: {
        p->state = S_SYNC1;
        uint16_t crc = 0xFFFFu;
        crc = crc16_update(crc, p->type);
        crc = crc16_update(crc, p->seq);
        crc = crc16_update(crc, p->len);
        for (uint8_t i = 0; i < p->len; i++)
            crc = crc16_update(crc, p->payload[i]);
        if (crc != (uint16_t)(p->crc_lo | ((uint16_t)b << 8))) {
            p->crc_errors++;
            break;
        }
        p->payload[p->len] = 0;
        return 1;
    }
    default:
        p->state = S_SYNC1;
        break;
    }
    return 0;
}
