"""RCP ("Reader Control Protocol") framing - shared by every transport this
reader speaks (USB_HID keystroke mode has its own module since it's a
completely different wire format, but RS485/USB-serial/WiFi/TCPIP all carry
these exact same binary frames per the vendor's own protocol spec: "MM Version
Reader Control Protocol_v1.2.pdf", bundled in this project under
THESIS PAPER/TECHNICAL SPECIFICATIONS OF READER-WRITERS/UHF-RFID-READER-ANTENNA/
sdk_UHF Reader/900MHz UHF Reader SDK(MM)/Document(文档)/. Cross-checked against
that SDK's own Java reference client (SioNetClient.java) for the real-world
connection defaults.

Frame layout (every multi-byte field is sent MSB-first except ADDR, which is
LSB then MSB per the spec's own tables):

    SOI(1) | ADDR(2) | CID1(1) | CID2_or_RTN(1) | LENGTH(1) | INFO(LENGTH) | CHECKSUM(1)

SOI is 0x7C for a command (host -> reader) and 0xCC for a response
(reader -> host). This module only needs to READ responses - the reader is
run in Active work mode, so it pushes "Read Type C UII" (CID1=0x20) frames on
its own the moment a tag is seen; nothing needs to be sent to trigger a read
(see the protocol doc's section 4.1, which states this explicitly).
"""

CMD_SOI = 0x7C
RESP_SOI = 0xCC

CID1_READ_UII = 0x20

# RTN 0x02 ("response message for a command") and 0x05 ("auto send to SU")
# are both used across the spec's own examples for a CID1=0x20 tag payload --
# the worked example in section 4.1.2 shows 0x02 even though the section's
# intro text says an unsolicited active-mode push carries 0x05. Since we
# never send the read command ourselves (Active mode), either one reaching us
# means "here's a tag," so both are treated as a tag frame; the response
# variant (INFO = ANT + STC + RTC, no EPC - section 4.1.3) uses RTN 0x00.
TAG_FRAME_RTNS = (0x02, 0x05)


def checksum(data):
    """Two's-complement of the sum of every byte, mod 256 - straight port of
    the spec's own reference C function (section 2.3). `data` is every byte
    of the frame EXCEPT the checksum byte itself."""
    total = sum(data) & 0xFF
    return (~total + 1) & 0xFF


def build_frame(addr, cid1, cid2, info=b""):
    """Builds a command frame (SOI=0x7C). Not needed for the passive listener
    (Active mode never requires sending a read command) but kept here since
    it's the natural counterpart to parse_frames() and any future on-demand
    command (e.g. Set Output Mode) will need it."""
    body = bytes([CMD_SOI, addr & 0xFF, (addr >> 8) & 0xFF, cid1, cid2, len(info)]) + bytes(info)
    return body + bytes([checksum(body)])


def parse_frames(buffer):
    """Extracts every complete, checksum-valid frame found at the start of
    `buffer`, returning (frames, remainder) where `frames` is a list of
    (cid1, rtn_or_cid2, info_bytes) tuples and `remainder` is whatever's left
    in `buffer` for the next call (a partial frame still arriving, or bytes
    ahead of the first frame that had to be dropped to resync).

    Byte-stream framing has no unique start-of-frame marker other than "the
    checksum happens to work out" - SOI (0x7C/0xCC) can appear inside a
    frame's own ADDR/INFO bytes too. So: scan for a SOI byte, tentatively
    read LENGTH from where a frame header would put it, check whether a full
    frame's worth of bytes has actually arrived yet, and verify the
    checksum. A checksum mismatch means that SOI was a false match (or the
    stream lost sync some other way) - drop just that one byte and keep
    scanning, rather than discarding everything read so far.
    """
    frames = []
    i = 0
    n = len(buffer)
    while i < n:
        if buffer[i] not in (CMD_SOI, RESP_SOI):
            i += 1
            continue
        if i + 6 > n:
            break  # not enough bytes yet to even read LENGTH - wait for more
        length = buffer[i + 5]
        frame_len = 7 + length
        if i + frame_len > n:
            break  # header's here, but the body/checksum hasn't fully arrived yet
        frame = buffer[i:i + frame_len]
        if checksum(frame[:-1]) != frame[-1]:
            i += 1  # false SOI match or desync - drop one byte and rescan
            continue
        cid1 = frame[3]
        rtn_or_cid2 = frame[4]
        info = bytes(frame[6:6 + length])
        frames.append((cid1, rtn_or_cid2, info))
        i += frame_len
    return frames, bytes(buffer[i:])


def extract_tag_id(cid1, rtn, info):
    """Given one parsed frame, returns the tag's EPC as an uppercase hex
    string if this frame is a "Read Type C UII" tag payload, else None.

    INFO layout for this frame (section 4.1.2): ANT(1) + PC(2) + EPC(N) +
    RSSI(1) - EPC's own length isn't stored anywhere explicit, but since
    every other field in INFO has a fixed size, N falls out of INFO's total
    length: N = len(INFO) - 4. That matches the worked example in the spec
    (len(INFO)=0x10=16, EPC=12 bytes -> 16-4=12) without needing to decode
    the PC word's bit-packed length field at all.
    """
    if cid1 != CID1_READ_UII or rtn not in TAG_FRAME_RTNS:
        return None
    if len(info) < 4:
        return None
    epc = info[3:len(info) - 1]
    if not epc:
        return None
    return epc.hex().upper()
