"""Unit tests for pi_controller/rcp_protocol.py - pure byte-parsing logic,
no hardware/network involved. Frame bytes are taken straight from the
vendor's own protocol spec examples ("MM Version Reader Control Protocol
v1.2", section 4.1.2's worked "Tag Response" example) so a mistake here would
mean a mismatch against the reader's actual documented wire format, not just
an internally-consistent-but-wrong assumption."""

from pi_controller import rcp_protocol


def test_checksum_matches_the_spec_worked_example():
    # Section 2.3's own worked example: "CC 02 01 B1 22 04 BB 12 02 03" -> 0x88
    data = bytes.fromhex("CC0201B12204BB120203")
    assert rcp_protocol.checksum(data) == 0x88


def _spec_tag_response_frame():
    # Section 4.1.2's worked example: ANT=0x00, PC=0x3000,
    # EPC=0xE2003411B802011383258566 (12 bytes), RSSI=0xC9.
    # HEAD ADDR(LSB) ADDR(MSB) CID1 RTN LENGTH ANT PC(2)  EPC(12 bytes)                RSSI
    #  CC    FF        FF      20   02   10    00  3000   E2003411B802011383258566    C9
    body = bytes.fromhex("CCFFFF20021000" "3000" "E2003411B802011383258566" "C9")
    return body + bytes([rcp_protocol.checksum(body)])


def test_parse_frames_extracts_the_spec_worked_example_frame():
    frame_bytes = _spec_tag_response_frame()
    frames, remainder = rcp_protocol.parse_frames(frame_bytes)
    assert remainder == b""
    assert len(frames) == 1
    cid1, rtn, info = frames[0]
    assert cid1 == rcp_protocol.CID1_READ_UII
    assert rtn == 0x02
    assert info[0] == 0x00  # ANT


def test_extract_tag_id_returns_the_epc_as_uppercase_hex():
    frame_bytes = _spec_tag_response_frame()
    frames, _ = rcp_protocol.parse_frames(frame_bytes)
    cid1, rtn, info = frames[0]
    tag_id = rcp_protocol.extract_tag_id(cid1, rtn, info)
    assert tag_id == "E2003411B802011383258566"


def test_parse_frames_handles_a_frame_split_across_two_reads():
    frame_bytes = _spec_tag_response_frame()
    frames1, buffer = rcp_protocol.parse_frames(frame_bytes[:5])
    assert frames1 == []
    frames2, buffer = rcp_protocol.parse_frames(buffer + frame_bytes[5:])
    assert len(frames2) == 1
    assert buffer == b""


def test_parse_frames_extracts_two_back_to_back_frames_in_one_buffer():
    frame_bytes = _spec_tag_response_frame()
    frames, remainder = rcp_protocol.parse_frames(frame_bytes + frame_bytes)
    assert len(frames) == 2
    assert remainder == b""


def test_parse_frames_resyncs_past_a_false_soi_match_with_a_bad_checksum():
    frame_bytes = _spec_tag_response_frame()
    # A byte that happens to equal RESP_SOI (0xCC), immediately followed by a
    # complete-but-bogus 7-byte header+checksum (LENGTH=0x00, so it doesn't
    # overrun into "wait for more data" territory) whose checksum can't
    # possibly match - this is what a genuine desync recovers from, as
    # opposed to a real frame that's still arriving (see the "incomplete
    # trailing frame" test below for that case, which correctly waits
    # instead of discarding anything).
    false_soi = bytes.fromhex("CC0000000000") + b"\x00"
    frames, remainder = rcp_protocol.parse_frames(b"\x01\x02" + false_soi + frame_bytes)
    assert len(frames) == 1
    assert remainder == b""


def test_parse_frames_leaves_an_incomplete_trailing_frame_in_the_buffer():
    frame_bytes = _spec_tag_response_frame()
    partial = frame_bytes[:-2]  # header + most of INFO, but not the full frame
    frames, remainder = rcp_protocol.parse_frames(partial)
    assert frames == []
    assert remainder == partial


def test_extract_tag_id_ignores_non_tag_frames():
    # Section 4.1.3's "Response" (STC/RTC summary, RTN=0x00) carries no EPC.
    assert rcp_protocol.extract_tag_id(rcp_protocol.CID1_READ_UII, 0x00, b"\x00\x27\x27") is None
    # A different CID1 entirely (e.g. some other command's response).
    assert rcp_protocol.extract_tag_id(0x50, 0x02, b"\x1a") is None


def test_build_frame_round_trips_through_parse_frames():
    frame_bytes = rcp_protocol.build_frame(0xFFFF, rcp_protocol.CID1_READ_UII, 0x00)
    frames, remainder = rcp_protocol.parse_frames(frame_bytes)
    assert remainder == b""
    assert frames == [(rcp_protocol.CID1_READ_UII, 0x00, b"")]
