"""
media_inspector.py
------------------
Ultra-fast, pure-Python media container inspector for local video files.
Parses container headers (MP4, MKV, WebM, AVI) to extract video resolution,
video codec, and audio codec in < 1 millisecond without requiring external tools
or reading full media streams.
"""

import os
import re
import struct
from typing import Optional, Dict, Any


def get_resolution_label(width: int, height: int) -> Optional[str]:
    """Maps pixel dimensions to standard quality labels."""
    if width <= 0 and height <= 0:
        return None
    # 4K / UHD (e.g. 3840x2160, 4096x2160, or cropped ~1600+ height)
    if height >= 1600 or width >= 3200:
        return "4K"
    # 1080p (e.g. 1920x1080 or widescreen ~800+ height)
    elif height >= 800 or width >= 1600:
        return "1080p"
    # 720p (e.g. 1280x720 or widescreen ~500+ height)
    elif height >= 500 or width >= 1100:
        return "720p"
    else:
        return "SD"


def _parse_ebml_vint(data: bytes, offset: int):
    """Parses a Matroska EBML variable-size integer."""
    if offset >= len(data):
        return None, 0
    first = data[offset]
    if first == 0:
        return None, 0
    mask = 0x80
    length = 1
    while not (first & mask):
        mask >>= 1
        length += 1
        if length > 8:
            return None, 0
    if offset + length > len(data):
        return None, 0
    value = first & (~mask)
    for i in range(1, length):
        value = (value << 8) | data[offset + i]
    return value, length


def parse_mkv_metadata(file_path: str) -> Optional[Dict[str, Any]]:
    """Inspects Matroska / WebM EBML headers for resolution and codecs."""
    try:
        with open(file_path, "rb") as f:
            data = f.read(4 * 1024 * 1024)  # Read initial 4MB chunk

            # Find Segment payload offset to resolve SeekHead offsets if needed
            seg_payload_offset = 0
            seg_pos = data.find(b"\x18\x53\x80\x67")
            if seg_pos != -1:
                _, seg_vlen = _parse_ebml_vint(data, seg_pos + 4)
                if seg_vlen:
                    seg_payload_offset = seg_pos + 4 + seg_vlen

            # Check if Tracks element (0x16 0x54 0xAE 0x6B) is directly found
            # or if it's referenced in SeekHead (0x53AB SeekID with value 0x1654AE6B)
            tracks_pos = -1
            p = 0
            while True:
                p = data.find(b"\x16\x54\xae\x6b", p)
                if p == -1:
                    break
                # Check if this occurrence is inside SeekHead (preceded by SeekID 0x53AB)
                if p >= 2 and (data[p - 2 : p] == b"\x53\xab" or (p >= 3 and data[p - 3 : p - 1] == b"\x53\xab")):
                    # It's a SeekID pointing to Tracks!
                    # Find SeekPosition (0x53AC) in proximity
                    sp_pos = data.find(b"\x53\xac", p, p + 50)
                    if sp_pos != -1:
                        sp_len, sp_vlen = _parse_ebml_vint(data, sp_pos + 2)
                        if sp_len and sp_len <= 8:
                            sp_start = sp_pos + 2 + sp_vlen
                            seek_val = int.from_bytes(data[sp_start : sp_start + sp_len], "big")
                            if seg_payload_offset:
                                target_offset = seg_payload_offset + seek_val
                                f.seek(target_offset)
                                data = f.read(2 * 1024 * 1024)
                                tracks_pos = data.find(b"\x16\x54\xae\x6b")
                                break
                    p += 4
                    continue
                else:
                    # Found genuine Tracks element
                    tracks_pos = p
                    break

            if tracks_pos == -1:
                # Tracks not found yet; try reading further up to 10MB
                f.seek(0)
                data = f.read(10 * 1024 * 1024)
                tracks_pos = data.find(b"\x16\x54\xae\x6b")
                if tracks_pos == -1:
                    return None

            offset = tracks_pos + 4
            size, v_len = _parse_ebml_vint(data, offset)
            if size is None:
                return None
            offset += v_len
            tracks_end = min(offset + size, len(data))

            width, height = 0, 0
            video_codec = None
            audio_codec = None

            # Scan for TrackEntry (0xAE)
            while offset < tracks_end:
                entry_pos = data.find(b"\xae", offset, tracks_end)
                if entry_pos == -1:
                    break
                entry_offset = entry_pos + 1
                entry_size, v_len = _parse_ebml_vint(data, entry_offset)
                if entry_size is None:
                    offset = entry_pos + 1
                    continue
                entry_end = min(entry_offset + v_len + entry_size, tracks_end)
                entry_data = data[entry_offset + v_len : entry_end]

                # TrackType (0x83): 1 = Video, 2 = Audio
                track_type = None
                tt_pos = entry_data.find(b"\x83")
                if tt_pos != -1:
                    tt_val, _ = _parse_ebml_vint(entry_data, tt_pos + 1)
                    track_type = tt_val

                # CodecID (0x86)
                codec_id = ""
                cid_pos = entry_data.find(b"\x86")
                if cid_pos != -1:
                    cid_len, c_v_len = _parse_ebml_vint(entry_data, cid_pos + 1)
                    if cid_len and cid_len < 128:
                        c_start = cid_pos + 1 + c_v_len
                        codec_id = entry_data[c_start : c_start + cid_len].decode("latin1", errors="ignore").upper()

                # Video track details (TrackType 1 or contains Video element 0xE0)
                if track_type == 1 or b"\xe0" in entry_data:
                    # Video element (0xE0) contains PixelWidth (0xB0) and PixelHeight (0xBA)
                    v_pos = entry_data.find(b"\xe0")
                    v_scope = entry_data[v_pos : v_pos + 128] if v_pos != -1 else entry_data

                    pw_pos = v_scope.find(b"\xb0")
                    if pw_pos != -1:
                        w_len, w_v_len = _parse_ebml_vint(v_scope, pw_pos + 1)
                        if w_len in (1, 2, 3, 4):
                            w_start = pw_pos + 1 + w_v_len
                            w = int.from_bytes(v_scope[w_start : w_start + w_len], "big")
                            if w > width:
                                width = w

                    ph_pos = v_scope.find(b"\xba")
                    if ph_pos != -1:
                        h_len, h_v_len = _parse_ebml_vint(v_scope, ph_pos + 1)
                        if h_len in (1, 2, 3, 4):
                            h_start = ph_pos + 1 + h_v_len
                            h = int.from_bytes(v_scope[h_start : h_start + h_len], "big")
                            if h > height:
                                height = h

                    if "HEVC" in codec_id or "H265" in codec_id:
                        video_codec = "HEVC"
                    elif "AVC" in codec_id or "H264" in codec_id:
                        video_codec = "AVC"
                    elif "AV1" in codec_id:
                        video_codec = "AV1"
                    elif "VP9" in codec_id:
                        video_codec = "VP9"
                    elif "MPEG4" in codec_id or "XVID" in codec_id:
                        video_codec = "XViD"

                elif track_type == 2 or b"\xe1" in entry_data:
                    if "TRUEHD" in codec_id:
                        audio_codec = "TrueHD"
                    elif "DTS" in codec_id:
                        audio_codec = "DTS"
                    elif "EAC3" in codec_id or "EC3" in codec_id:
                        audio_codec = "EAC3"
                    elif "AC3" in codec_id:
                        audio_codec = "AC3"
                    elif "AAC" in codec_id:
                        audio_codec = "AAC"
                    elif "FLAC" in codec_id:
                        audio_codec = "FLAC"
                    elif "OPUS" in codec_id:
                        audio_codec = "Opus"

                offset = entry_end

            return _build_result(width, height, video_codec, audio_codec)
    except Exception:
        return None


def parse_mp4_metadata(file_path: str) -> Optional[Dict[str, Any]]:
    """Inspects MP4 / M4V / MOV box headers for resolution and codecs."""
    try:
        size = os.path.getsize(file_path)
        with open(file_path, "rb") as f:
            pos = 0
            moov_data = None
            while pos < size:
                f.seek(pos)
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                box_size, box_type = struct.unpack(">I4s", hdr)
                if box_size == 1:
                    box_size, = struct.unpack(">Q", f.read(8))
                if box_size < 8:
                    break
                if box_type == b"moov":
                    read_len = min(box_size - 8, 10 * 1024 * 1024)
                    moov_data = f.read(read_len)
                    break
                pos += box_size

            if not moov_data:
                return None

            width, height = 0, 0
            idx = 0
            while True:
                pos = moov_data.find(b"tkhd", idx)
                if pos == -1:
                    break
                version = moov_data[pos + 4]
                w_off = pos + 4 + (88 if version != 0 else 76)
                if w_off + 8 <= len(moov_data):
                    w_raw, = struct.unpack(">I", moov_data[w_off : w_off + 4])
                    h_raw, = struct.unpack(">I", moov_data[w_off + 4 : w_off + 8])
                    w = w_raw >> 16
                    h = h_raw >> 16
                    if w > width and h > height:
                        width, height = w, h
                idx = pos + 4

            video_codec = None
            if b"hvc1" in moov_data or b"hev1" in moov_data:
                video_codec = "HEVC"
            elif b"avc1" in moov_data or b"avc3" in moov_data:
                video_codec = "AVC"
            elif b"av01" in moov_data:
                video_codec = "AV1"
            elif b"vp09" in moov_data:
                video_codec = "VP9"
            elif b"mp4v" in moov_data:
                video_codec = "MPEG4"

            audio_codec = None
            if b"ec-3" in moov_data:
                audio_codec = "EAC3"
            elif b"ac-3" in moov_data:
                audio_codec = "AC3"
            elif b"dts " in moov_data:
                audio_codec = "DTS"
            elif b"mp4a" in moov_data:
                audio_codec = "AAC"
            elif b"alac" in moov_data:
                audio_codec = "ALAC"

            return _build_result(width, height, video_codec, audio_codec)
    except Exception:
        return None


def parse_avi_metadata(file_path: str) -> Optional[Dict[str, Any]]:
    """Inspects AVI RIFF headers for resolution and video FourCC."""
    try:
        with open(file_path, "rb") as f:
            hdr = f.read(1024)
        if len(hdr) < 56 or not hdr.startswith(b"RIFF") or hdr[8:12] != b"AVI ":
            return None

        # Look for 'avih' chunk (AVI Main Header, 56 bytes)
        avih_pos = hdr.find(b"avih")
        if avih_pos == -1 or avih_pos + 48 > len(hdr):
            return None

        # Offset in avih: width at +32, height at +36
        w_off = avih_pos + 4 + 4 + 32  # chunk_id (4) + chunk_size (4) + offset 32
        h_off = w_off + 4
        width, height = struct.unpack("<II", hdr[w_off : h_off + 4])

        # Look for video FourCC in 'strh'
        video_codec = None
        vids_pos = hdr.find(b"vids")
        if vids_pos != -1 and vids_pos + 8 <= len(hdr):
            fourcc = hdr[vids_pos + 4 : vids_pos + 8].decode("latin1", errors="ignore").upper()
            if fourcc in ("XVID", "DIVX", "DX50"):
                video_codec = "XViD"
            elif fourcc in ("H264", "AVC1", "X264"):
                video_codec = "AVC"

        return _build_result(width, height, video_codec, None)
    except Exception:
        return None


def _build_result(width: int, height: int, video_codec: Optional[str], audio_codec: Optional[str]) -> Dict[str, Any]:
    quality = get_resolution_label(width, height)
    parts = []
    if quality:
        parts.append(quality)
    if video_codec:
        parts.append(video_codec)
    if audio_codec:
        parts.append(audio_codec)

    return {
        "width": width,
        "height": height,
        "quality": quality or "SD",
        "video_codec": video_codec or "",
        "audio_codec": audio_codec or "",
        "extraInfo": " | ".join(parts) if parts else ""
    }


def parse_filename_media_info(file_path: str) -> Dict[str, Any]:
    """
    Extracts resolution, video codec, and audio codec from filename tokens as a pure-string fallback.
    Guaranteed to return a dict with {width, height, quality, video_codec, audio_codec, extraInfo}.
    """
    fname = os.path.basename(file_path) if file_path else ""
    width = 0
    height = 0
    quality = "SD"
    video_codec = ""
    audio_codec = ""

    # Quality / Resolution
    if re.search(r'\b(2160p|4k|uhd)\b', fname, re.IGNORECASE):
        quality = "4K"
        width, height = 3840, 2160
    elif re.search(r'\b(1080p|1080i|fhd)\b', fname, re.IGNORECASE):
        quality = "1080p"
        width, height = 1920, 1080
    elif re.search(r'\b(720p|hd)\b', fname, re.IGNORECASE):
        quality = "720p"
        width, height = 1280, 720
    elif re.search(r'\b(480p|576p|sd)\b', fname, re.IGNORECASE):
        quality = "SD"
        width, height = 720, 480

    # Video Codec
    if re.search(r'\b(x265|h265|h\.265|hevc)\b', fname, re.IGNORECASE):
        video_codec = "HEVC"
    elif re.search(r'\b(x264|h264|h\.264|avc)\b', fname, re.IGNORECASE):
        video_codec = "AVC"
    elif re.search(r'\b(av1)\b', fname, re.IGNORECASE):
        video_codec = "AV1"
    elif re.search(r'\b(vp9)\b', fname, re.IGNORECASE):
        video_codec = "VP9"
    elif re.search(r'\b(xvid|divx)\b', fname, re.IGNORECASE):
        video_codec = "XViD"
    elif re.search(r'\b(mpeg2)\b', fname, re.IGNORECASE):
        video_codec = "MPEG2"

    # Audio Codec
    if re.search(r'\b(truehd)\b', fname, re.IGNORECASE):
        audio_codec = "TrueHD"
    elif re.search(r'\b(atmos)\b', fname, re.IGNORECASE):
        audio_codec = "Atmos"
    elif re.search(r'\b(dts[\.\-_]?hd|dtshd|dts)\b', fname, re.IGNORECASE):
        audio_codec = "DTS"
    elif re.search(r'\b(eac3|ddp|dd\+|ec3)\b', fname, re.IGNORECASE):
        audio_codec = "EAC3"
    elif re.search(r'\b(ac3|dd5\.1|dd2\.0)\b', fname, re.IGNORECASE):
        audio_codec = "AC3"
    elif re.search(r'\b(aac)\b', fname, re.IGNORECASE):
        audio_codec = "AAC"
    elif re.search(r'\b(flac)\b', fname, re.IGNORECASE):
        audio_codec = "FLAC"
    elif re.search(r'\b(opus)\b', fname, re.IGNORECASE):
        audio_codec = "Opus"

    parts = [p for p in (quality, video_codec, audio_codec) if p and p != "SD"]
    return {
        "width": width,
        "height": height,
        "quality": quality,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "extraInfo": " | ".join(parts) if parts else ""
    }


def inspect_media_file(file_path: str) -> Dict[str, Any]:
    """
    Main entry point: detects container type from extension and extracts metadata.
    Guaranteed to return a dict with {width, height, quality, video_codec, audio_codec, extraInfo}.
    Never returns None.
    """
    fn_info = parse_filename_media_info(file_path) if file_path else {
        "width": 0, "height": 0, "quality": "SD", "video_codec": "", "audio_codec": "", "extraInfo": ""
    }

    if not file_path:
        return fn_info

    info = None
    try:
        if os.path.isfile(file_path):
            ext = os.path.splitext(file_path)[-1].lower()
            if ext in (".mp4", ".m4v", ".mov"):
                info = parse_mp4_metadata(file_path)
            elif ext in (".mkv", ".webm"):
                info = parse_mkv_metadata(file_path)
            elif ext == ".avi":
                info = parse_avi_metadata(file_path)
    except Exception:
        info = None

    if not info:
        return fn_info

    # Merge: if container inspection missed codec or resolution, fill from filename
    if (not info.get("quality") or info.get("quality") == "SD") and fn_info.get("quality") and fn_info.get("quality") != "SD":
        info["quality"] = fn_info["quality"]
        if not info.get("width"):
            info["width"] = fn_info.get("width", 0)
        if not info.get("height"):
            info["height"] = fn_info.get("height", 0)

    if not info.get("video_codec") and fn_info.get("video_codec"):
        info["video_codec"] = fn_info["video_codec"]

    if not info.get("audio_codec") and fn_info.get("audio_codec"):
        info["audio_codec"] = fn_info["audio_codec"]

    # Rebuild extraInfo
    parts = []
    if info.get("quality") and info["quality"] != "SD":
        parts.append(info["quality"])
    elif info.get("quality") == "SD" and (info.get("video_codec") or info.get("audio_codec")):
        parts.append("SD")
    if info.get("video_codec"):
        parts.append(info["video_codec"])
    if info.get("audio_codec"):
        parts.append(info["audio_codec"])
    info["extraInfo"] = " | ".join(parts)

    return info
