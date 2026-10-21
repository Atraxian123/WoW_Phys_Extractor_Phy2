#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""m2_extract_phys_py26.py - (Python 2.6 / 2.7 / 3.x edition) extract the physics chunk of retail .m2 models into .phys files.

One file, standard library only. Works on Python 2.6.6 and newer (also 3.x); the window needs
plain Tkinter, which those versions ship with. For every model it writes a .phys named after it (Dagger.m2 ->
Dagger.phys); with --phy2 it also writes Dagger.phy2 beside it, the PHY2 table Equip Plus reads.

    python m2_extract_phys_py26.py                      open the window
    python m2_extract_phys_py26.py cape.m2 helm.m2      -> cape.phys, helm.phys next to each model
    python m2_extract_phys_py26.py C:\models -o C:\phys every .m2 below the folder; the sub-folders are
                                                   mirrored under C:\phys so same-named models never clash
    python m2_extract_phys_py26.py helm.m2 --resolve C:\by_fdid
    python m2_extract_phys_py26.py Dagger.m2 --phy2      -> Dagger.phys + Dagger.phy2
    python m2_extract_phys_py26.py Dagger.phys           -> Dagger.phy2 (from an existing .phys)
    python m2_extract_phys_py26.py C:\phys --from-phys   -> a .phy2 for every .phys below the folder
    python m2_extract_phys_py26.py C:\models -o C:\phys --phy2-all C:\phys\WXLEquipPlusPhysics.phy2
                                                   (one combined table of every model, as well)

A retail (chunked) model carries its physics inline (PFDC chunk), which is written out
byte-for-byte, or names a separate .phys by file-data id (PFID). For the second kind the .phys is
not inside the model: put it in a folder as <fdid>.phys and pass that folder with --resolve.

PHY2 (opt-in): rows are keyed by archive path, lower case with backslashes. The path is taken
relative to --root (default: the folder you added; for a lone file, just its name); --key-prefix
puts a folder in front. Body origins are the file's own values (no helm shift is applied). Models
the engine cannot take (box shapes, unknown joint types, a bone the model lacks) still get their
.phys but are left out of the table, with the reason listed.

From an existing .phys: give .phys files (or, with --from-phys, folders of them) instead of models
and only the .phy2 is written (Dagger.phys -> Dagger.phy2, key "dagger.m2" - the .phys path with
.m2 in place of .phys). With no model to read, the bone check is not possible.

Exit codes: 0 done; 2 done with some models skipped (listed); 1 nothing written.
"""
from __future__ import print_function

import optparse
import os
import struct
import sys
import threading

try:
    import Queue as queue          # Python 2
except ImportError:
    import queue                   # Python 3

try:   # only the window needs Tk; the command line works without
    import Tkinter as tk           # Python 2
    import tkFileDialog as filedialog
    import tkMessageBox as messagebox
except ImportError:
    try:
        import tkinter as tk       # Python 3
        from tkinter import filedialog, messagebox
    except ImportError:
        tk = filedialog = messagebox = None


def _b(s):
    """text -> bytes (latin-1); bytes pass through (on Python 2 a plain str is already bytes)."""
    return s if isinstance(s, bytes) else s.encode("latin-1")


def _tag(four):
    """A chunk tag stored byte-reversed ("SYHP" is PHYS) as a native str, on 2 and 3 alike."""
    return "".join(chr(c) for c in bytearray(four[::-1]))


def _makedirs(path):
    if path and not os.path.isdir(path):
        try:
            os.makedirs(path)
        except OSError:
            if not os.path.isdir(path):
                raise


# --------------------------------------------------------------------------------------------------
# reading the model

class M2Error(Exception):
    pass


def top_chunks(model):
    """[(tag, payload offset, size)] of a chunked model; stops at the first chunk that overruns."""
    out, o = [], 0
    while o + 8 <= len(model):
        tag = model[o:o + 4]
        n = struct.unpack_from("<I", model, o + 4)[0]
        if o + 8 + n > len(model):
            break
        out.append((tag, o + 8, n))
        o += 8 + n
    return out


def inspect_model(model):
    """dict(kind 'legacy'|'none'|'inline'|'external', phys bytes|None, fdid int|None)."""
    if model[:4] == b"MD20":
        return {"kind": "legacy", "phys": None, "fdid": None, "md20": model}
    chunks = top_chunks(model)
    if not chunks or chunks[0][0] != b"MD21":
        raise M2Error("not an M2 model (no MD20 / MD21 at the start)")
    if chunks[-1][1] + chunks[-1][2] != len(model):
        raise M2Error("the file is truncated or damaged: a chunk runs past its end")
    phys = fdid = None
    md20 = b""
    for tag, at, n in chunks:
        if tag == b"MD21":
            md20 = model[at:at + n]
        if tag == b"PFDC" and phys is None:
            phys = model[at:at + n]
        elif tag == b"PFID" and n >= 4:
            fdid = struct.unpack_from("<I", model, at)[0]
    if phys is not None:
        return {"kind": "inline", "phys": phys, "fdid": fdid, "md20": md20}
    if fdid:
        return {"kind": "external", "phys": None, "fdid": fdid, "md20": md20}
    return {"kind": "none", "phys": None, "fdid": None, "md20": md20}


def check_phys(blob):
    """Raises M2Error unless the blob walks cleanly as .phys chunks starting with PHYS.
    Returns ({chunk name: payload size}, end) where end is where the last chunk stops: the model
    pads its physics chunk to a multiple of 4, so a few zero bytes after `end` are padding."""
    if blob[:4] not in (b"PHYS", b"SYHP"):
        raise M2Error("the physics data does not start with a PHYS chunk")
    sizes, o = {}, 0
    while o + 8 <= len(blob):
        n = struct.unpack_from("<I", blob, o + 4)[0]
        if o + 8 + n > len(blob):
            raise M2Error("a physics chunk runs past the end of the data (truncated or encrypted)")
        sizes[_tag(blob[o:o + 4])] = n   # stored byte-reversed: "SYHP" is PHYS
        o += 8 + n
    if any(bytearray(blob[o:])):
        raise M2Error("%d stray byte(s) after the last physics chunk, and they are not padding" % (len(blob) - o))
    return sizes, o


def summary(sizes):
    b, s, j = sizes.get("BDY4", 0) // 0x30, sizes.get("SHP2", 0) // 0x20, sizes.get("JOIN", 0) // 16
    return "%d bodies, %d shapes, %d joints" % (b, s, j)


# --------------------------------------------------------------------------------------------------
# .phys decoding and PHY2 rows (the layout of wxl-equip-plus tools/physics_table.py)

BODY_TYPES = {0: 1, 1: 0}               # file type -> Domino: 0 kinematic (1), 1 dynamic (0), else static
SHAPE_TYPES = {2: 0, 1: 1, 3: 2}        # file SHP2 type -> Domino: sphere 0, capsule 1, hull 2 (box: none)
JOINT_TYPES = {0: 8, 1: 7, 2: 9, 3: 6, 4: 5, 5: 2}   # JOIN type -> Domino joint type


class PhysError(Exception):
    pass


def _chunks(blob):
    out = {}
    o = 0
    while o + 8 <= len(blob):
        tag = _tag(blob[o:o + 4])   # stored byte-reversed: "SYHP" is PHYS
        n = struct.unpack_from("<I", blob, o + 4)[0]
        out[tag] = blob[o + 8:o + 8 + n]
        o += 8 + n
    return out


def _f(b, o, n=1):
    v = struct.unpack_from("<%df" % n, b, o)
    return v if n > 1 else v[0]


def parse_phys(blob):
    """The .phys block as the engine's table rows want it: bodies, shapes, joints, hulls."""
    if blob[:4] not in (b"PHYS", b"SYHP"):
        raise PhysError("not a PHYS block")
    ch = _chunks(blob)
    if "BDY4" not in ch or "SHP2" not in ch or "JOIN" not in ch:
        raise PhysError("not a v6 file (needs BDY4, SHP2, JOIN): %s" % sorted(ch))

    bodies, shape_owner = [], {}
    b = ch["BDY4"]
    for i in range(len(b) // 0x30):
        o = i * 0x30
        typ, bone = struct.unpack_from("<HH", b, o)
        pos = _f(b, o + 4, 3)
        first = struct.unpack_from("<H", b, o + 0x10)[0]
        count = struct.unpack_from("<i", b, o + 0x14)[0]
        gravity, inertia, lin, ang, carry = _f(b, o + 0x18, 5)
        group = struct.unpack_from("<H", b, o + 0x2C)[0]
        for s in range(first, first + max(count, 0)):
            shape_owner[s] = i
        bodies.append({"type": BODY_TYPES.get(typ, 2), "bone": bone, "pos": list(pos), "gravity": gravity,
                       "inertia": inertia, "lin": lin, "ang": ang, "carry": carry, "group": group})

    caps = ch.get("CAPS", b"")
    sphs = ch.get("SPHS", b"")
    hulls = _hulls(ch.get("PLYT", b""))
    shapes = []
    s = ch["SHP2"]
    for i in range(len(s) // 0x20):
        o = i * 0x20
        st, si = struct.unpack_from("<hh", s, o)
        fr, rs, den = _f(s, o + 8, 3)
        if st not in SHAPE_TYPES:
            raise PhysError("shape %d is a box, which the engine does not have" % i)
        rec = {"body": shape_owner.get(i, 0), "type": SHAPE_TYPES[st], "hull": 0, "friction": fr,
               "restitution": rs, "density": den, "a": [0.0] * 3, "b": [0.0] * 3, "r": 0.0}
        if st == 1:
            if (si + 1) * 0x1C > len(caps):
                raise PhysError("capsule %d missing" % si)
            rec["a"], rec["b"] = list(_f(caps, si * 0x1C, 3)), list(_f(caps, si * 0x1C + 12, 3))
            rec["r"] = _f(caps, si * 0x1C + 24)
        elif st == 2:
            if (si + 1) * 16 > len(sphs):
                raise PhysError("sphere %d missing" % si)
            rec["a"], rec["r"] = list(_f(sphs, si * 16, 3)), _f(sphs, si * 16 + 12)
        else:
            if si >= len(hulls):
                raise PhysError("hull %d missing" % si)
            rec["hull"] = si
        shapes.append(rec)

    subs = {}
    for tag, kind, size in (("SPHJ", 0, 28), ("SHJ2", 1, 0x7C), ("WLJ3", 2, 0x74), ("REV2", 3, 0x78)):
        if tag in ch:
            subs[kind] = (ch[tag], size)
    joints = []
    J = ch["JOIN"]
    for i in range(len(J) // 16):
        a, bb, _pad, jt, jid = struct.unpack_from("<II4shh", J, i * 16)
        if jt not in subs:
            raise PhysError("joint %d has type %d, not supported here" % (i, jt))
        d, size = subs[jt]
        o = jid * size
        if o + size > len(d):
            raise PhysError("joint %d record missing" % i)
        rec = {"type": JOINT_TYPES[jt], "a": a, "b": bb, "mode": 0, "pa": [0.0] * 3, "pb": [0.0] * 3,
               "fa": [1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0], "fb": [1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0], "f": [0.0] * 6}
        if jt == 0:                       # spherical: two anchors
            rec["pa"], rec["pb"] = list(_f(d, o, 3)), list(_f(d, o + 12, 3))
        else:                             # frames: 3x3 (axis images as rows) + position, A then B
            rec["fa"], rec["pa"] = list(_f(d, o, 9)), list(_f(d, o + 0x24, 3))
            rec["fb"], rec["pb"] = list(_f(d, o + 0x30, 9)), list(_f(d, o + 0x54, 3))
            if jt == 1:                   # SHJ2: twistLo, twistHi, cone, maxTorque, mode, Hz, zeta
                tl, th, cone, mt = _f(d, o + 0x60, 4)
                rec["mode"] = struct.unpack_from("<b", d, o + 0x70)[0]
                hz, zeta = _f(d, o + 0x74, 2)
                rec["f"] = [tl, th, cone, mt, hz, zeta]
            elif jt == 3:                 # REV2: lower, upper, maxTorque, mode, Hz, zeta
                lo, hi, mt = _f(d, o + 0x60, 3)
                rec["mode"] = struct.unpack_from("<b", d, o + 0x6C)[0]
                hz, zeta = _f(d, o + 0x70, 2)
                rec["f"] = [lo, hi, mt, hz, zeta, 0.0]
            elif jt == 2:                 # WLJ3: angHz 0x60, linHz 0x64, angZeta 0x68, linZeta 0x6C
                ahz, lhz, az, lz = _f(d, o + 0x60, 4)
                rec["f"] = [ahz, az, lhz, lz, 0.0, 0.0]
        joints.append(rec)

    if len(bodies) > 255 or len(shapes) > 255 or len(joints) > 255 or len(hulls) > 255:
        raise PhysError("too many records for the table")
    for j in joints:
        if j["a"] >= len(bodies) or j["b"] >= len(bodies):
            raise PhysError("a joint names a body the file does not have")
    return {"bodies": bodies, "shapes": shapes, "joints": joints, "hulls": hulls}


def _hulls(b):
    if not b:
        return []
    n = struct.unpack_from("<I", b, 0)[0]
    heads = []
    for i in range(n):
        o = 4 + i * 0x50
        nv = struct.unpack_from("<I", b, o)[0]
        nf = struct.unpack_from("<I", b, o + 0x10)[0]
        ne = struct.unpack_from("<I", b, o + 0x28)[0]
        heads.append((nv, nf, ne, list(_f(b, o + 0x38, 3)), _f(b, o + 0x44), _f(b, o + 0x48)))
    out = []
    o = 4 + n * 0x50
    for nv, nf, ne, cen, vol, area in heads:
        if nv > 255 or nf > 255 or ne > 255:
            raise PhysError("hull too large for the table")
        v = [list(_f(b, o + k * 12, 3)) for k in range(nv)]
        o += nv * 12
        planes = [list(_f(b, o + k * 16, 4)) for k in range(nf)]
        o += nf * 16
        face = list(bytearray(b[o:o + nf]))
        o += nf
        edge = list(bytearray(b[o:o + ne * 4]))
        o += ne * 4
        out.append({"centroid": cen, "volume": vol, "area": area, "v": v, "planes": planes,
                    "face": face, "edge": edge})
    return out


def physics_blob(model_bytes, read_fdid):
    """A retail model's .phys: inline PFDC, else the file its PFID names (read_fdid(fdid) -> bytes)."""
    if model_bytes[:4] == b"MD20":
        return None   # legacy flat file: no chunks, no physics
    o = 0
    pfid = None
    while o + 8 <= len(model_bytes):
        tag = model_bytes[o:o + 4]
        n = struct.unpack_from("<I", model_bytes, o + 4)[0]
        if tag == b"PFDC":
            return model_bytes[o + 8:o + 8 + n]
        if tag == b"PFID":
            pfid = struct.unpack_from("<I", model_bytes, o + 8)[0]
        o += 8 + n
    return read_fdid(pfid) if pfid else None


# --------------------------------------------------------------------------------------------------
# writing the table

def norm_key(path):
    k = path.replace("/", "\\").lower()
    if k.endswith(".mdx") or k.endswith(".mdl"):
        k = k[:-4] + ".m2"
    return k


def fnv1a(s):
    h = 0x811C9DC5
    for c in bytearray(_b(s)):
        h = ((h ^ c) * 0x01000193) & 0xFFFFFFFF
    return h


def _pad4(buf):
    while len(buf) % 4:
        buf.append(0)


def encode_row(key, p, shift=(0.0, 0.0, 0.0)):
    out = bytearray()
    kb = _b(key)
    out += struct.pack("<H", len(kb)) + kb
    _pad4(out)
    out += struct.pack("<4B", len(p["bodies"]), len(p["shapes"]), len(p["joints"]), len(p["hulls"]))
    for b in p["bodies"]:
        pos = [b["pos"][i] + shift[i] for i in range(3)]
        out += struct.pack("<BBH3f5fHH", *([b["type"], 0, b["bone"]] + pos +
                           [b["gravity"], b["inertia"], b["lin"], b["ang"], b["carry"], b["group"], 0]))
    for s in p["shapes"]:
        out += struct.pack("<BBH3f3f3ff", *([s["body"], s["type"], s["hull"], s["friction"], s["restitution"],
                           s["density"]] + list(s["a"]) + list(s["b"]) + [s["r"]]))
    for j in p["joints"]:
        out += struct.pack("<BBBb3f3f9f9f6f", *([j["type"], j["a"], j["b"], j["mode"]] + list(j["pa"]) +
                           list(j["pb"]) + list(j["fa"]) + list(j["fb"]) + list(j["f"])))
    for h in p["hulls"]:
        out += struct.pack("<4B3fff", *([len(h["v"]), len(h["planes"]), len(h["edge"]) // 4, 0] +
                           list(h["centroid"]) + [h["volume"], h["area"]]))
        for v in h["v"]:
            out += struct.pack("<3f", *v)
        for pl in h["planes"]:
            out += struct.pack("<4f", *pl)
        out += bytearray(list(h["face"]) + list(h["edge"]))
        _pad4(out)
    _pad4(out)
    return bytes(out)


def write_table(rows):
    """rows: {key: encoded row}. Keys must already be normalised."""
    items = sorted(((fnv1a(k), k, r) for k, r in rows.items()), key=lambda t: (t[0], t[1]))
    head = bytearray(b"PHY2" + struct.pack("<I", len(items)))
    body = bytearray()
    base = 8 + 8 * len(items)
    for h, _k, r in items:
        head += struct.pack("<II", h, base + len(body))
        body += r
    return bytes(head + body)


def m2_internal_name(model):
    """The name stored inside an MD20 model (header 0x08: length, offset), '' when absent."""
    if model[:4] != b"MD20" or len(model) < 0x10:
        return ""
    n, o = struct.unpack_from("<II", model, 0x08)
    if not n or o + n > len(model):
        return ""
    return model[o:o + n].split(b"\0", 1)[0].decode("latin-1", "replace")


def m2_bone_count(model):
    if model[:4] != b"MD20" or len(model) < 0x34:
        return 0
    return struct.unpack_from("<I", model, 0x2C)[0]


def row_body(row):
    """A row without its key (the part two keys can share)."""
    n = struct.unpack_from("<H", row, 0)[0]
    at = 2 + n
    at += (4 - at % 4) % 4
    return row[at:]


def keyed(key, body):
    kb = _b(key)
    out = bytearray(struct.pack("<H", len(kb)) + kb)
    _pad4(out)
    return bytes(out) + body


# --------------------------------------------------------------------------------------------------
# driving it

def find_models(paths, ext=".m2"):
    """Yields (model path, folder it was found under or None); a model named twice is read once."""
    seen = set()
    for p in paths:
        if os.path.isdir(p):
            found = []
            for dp, _dn, fn in os.walk(p):
                found += [os.path.join(dp, f) for f in sorted(fn) if f.lower().endswith(ext)]
            base = p
        else:
            found, base = [p], None
        for f in found:
            k = os.path.normcase(os.path.realpath(f))
            if k not in seen:
                seen.add(k)
                yield f, base


def archive_key(path, root, prefix):
    rel = os.path.relpath(path, root) if root else os.path.basename(path)
    rel = rel.replace("/", "\\")
    if prefix:
        rel = prefix.strip("\\/").replace("/", "\\") + "\\" + rel
    if rel.lower().endswith(".phys"):   # no model here: the key is the model the .phys belongs to
        rel = rel[:-5] + ".m2"
    return norm_key(rel)


def phy2_rows(blob, md20, key):
    """{key: row} for one model (its path key, and the name stored inside the model);
    raises PhysError when the engine table cannot take it."""
    p = parse_phys(blob)
    nb = m2_bone_count(md20)
    need = max([b["bone"] for b in p["bodies"] if b["bone"] != 0xFFFF] or [0])
    if nb and need >= nb:
        raise PhysError("the physics names bone %d but the model has %d" % (need, nb))
    body = row_body(encode_row("", p))
    rows = {key: keyed(key, body)}
    inner = m2_internal_name(md20)
    if inner:   # the client may report the name stored inside the model
        ik = norm_key(inner if inner.lower().endswith((".m2", ".mdx")) else inner + ".m2")
        rows.setdefault(ik, keyed(ik, body))
    return rows


def cli_main(argv=None):
    ap = optparse.OptionParser(usage="%prog [options] inputs...", add_help_option=False)
    ap.add_option("-h", "--help", action="store_true", dest="help", default=False, help="show this help")
    ap.add_option("-o", "--out", dest="out", help="folder for the .phys files (default: next to each model)")
    ap.add_option("--resolve", metavar="DIR", dest="resolve",
                  help="folder holding <fdid>.phys, for models that only name theirs")
    ap.add_option("--from-phys", action="store_true", dest="from_phys", default=False,
                  help="folders are searched for existing .phys files (a .phy2 is made for each) instead of .m2 models")
    ap.add_option("--overwrite", action="store_true", dest="overwrite", default=False,
                  help="replace .phys / .phy2 files that already exist")
    ap.add_option("--phy2", action="store_true", dest="phy2", default=False,
                  help="also write <model name>.phy2 (the PHY2 table Equip Plus reads) next to each .phys")
    ap.add_option("--phy2-all", metavar="FILE", dest="phy2_all",
                  help="also put every model in one combined PHY2 table at FILE (e.g. WXLEquipPlusPhysics.phy2)")
    ap.add_option("--root", metavar="DIR", dest="root",
                  help="folder the PHY2 keys are relative to (default: each added folder)")
    ap.add_option("--key-prefix", dest="key_prefix", default="", help="folder to put in front of every PHY2 key")
    a, inputs = ap.parse_args(argv)
    if a.help:
        print(__doc__)
        print(ap.format_option_help())
        return 0
    if not inputs:
        ap.error("give at least one .m2 file or folder (see --help)")
    a.inputs = inputs

    seen = done = done2 = skipped = 0
    rows = {}
    for path, base in find_models(a.inputs, ".phys" if a.from_phys else ".m2"):
        seen += 1
        name = os.path.basename(path)
        if path.lower().endswith(".phys"):   # an existing .phys: only its .phy2 is made
            write_phys = False
            try:
                with open(path, "rb") as f:
                    blob = f.read()
                sizes, end = check_phys(blob)
                blob = blob[:end]
            except (OSError, IOError, M2Error) as e:
                print("  skipped %s: %s" % (name, e))
                skipped += 1
                continue
            info, where = {"md20": b""}, "existing .phys"
        else:
            write_phys = True
            try:
                with open(path, "rb") as f:
                    info = inspect_model(f.read())
            except (OSError, IOError, M2Error) as e:
                print("  skipped %s: %s" % (name, e))
                skipped += 1
                continue

            if info["kind"] in ("none", "legacy"):
                print("  %-40s no physics%s" % (name, " (legacy MD20 file)" if info["kind"] == "legacy" else ""))
                continue

            blob, where = info["phys"], "inline"
            if info["kind"] == "external":
                where = "external, file id %d" % info["fdid"]
                cand = os.path.join(a.resolve, "%d.phys" % info["fdid"]) if a.resolve else None
                if cand and os.path.isfile(cand):
                    with open(cand, "rb") as f:
                        blob = f.read()
                else:
                    print("  %-40s %s: not found; put %d.phys in a folder and pass it as --resolve"
                          % (name, where, info["fdid"]))
                    skipped += 1
                    continue
            try:
                sizes, end = check_phys(blob)
                blob = blob[:end]   # drop the zero padding the model rounds its chunk up with
            except M2Error as e:
                print("  skipped %s: %s" % (name, e))
                skipped += 1
                continue

        mine = {}   # this model's PHY2 rows
        want2 = a.phy2 or not write_phys
        if want2 or a.phy2_all:
            try:
                mine = phy2_rows(blob, info["md20"], archive_key(path, a.root or base, a.key_prefix))
                rows.update(mine)
            except PhysError as e:
                print("    not in the PHY2 table: %s" % e)

        if a.out and base:   # folder input: keep its sub-folders so same-named models never clash
            outdir = os.path.join(a.out, os.path.dirname(os.path.relpath(path, base)))
        else:
            outdir = a.out or os.path.dirname(os.path.abspath(path))
        stem = os.path.join(outdir, os.path.splitext(name)[0])   # Dagger.m2 -> Dagger.phys, Dagger.phy2
        _makedirs(outdir)
        wrote = []
        for ext, data in ((".phys", blob if write_phys else None), (".phy2", write_table(mine) if (want2 and mine) else None)):
            if data is None:
                continue
            if os.path.exists(stem + ext) and not a.overwrite:
                print("  %-40s not written: %s exists (use overwrite)" % (name, stem + ext))
                skipped += 1
                continue
            with open(stem + ext, "wb") as f:
                f.write(data)
            wrote.append(os.path.basename(stem + ext))
            if ext == ".phys":
                done += 1
            else:
                done2 += 1
        if wrote:
            print("  %-40s %s: %s -> %s" % (name, where, summary(sizes), " + ".join(wrote)))

    if a.phy2_all:
        if rows:
            _makedirs(os.path.dirname(os.path.abspath(a.phy2_all)))
            with open(a.phy2_all, "wb") as f:
                f.write(write_table(rows))
            print("combined PHY2 table: %d key(s) -> %s" % (len(rows), a.phy2_all))
        else:
            print("combined PHY2 table: no model could go in it, nothing written")
    print("%d model(s) read, %d .phys written%s, %d skipped"
          % (seen, done, (", %d .phy2 written" % done2) if (a.phy2 or done2) else "", skipped))
    if not done and not done2 and not (a.phy2_all and rows):
        return 1
    return 2 if skipped else 0


# --------------------------------------------------------------------------------------------------
# the window

class _QueueWriter(object):
    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(s)

    def flush(self):
        pass


class App(object):
    """Plain Tkinter (no ttk, which Python 2.6 does not have)."""

    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.busy = False
        f = tk.Frame(root, padx=10, pady=10)
        f.pack(fill="both", expand=True)
        self.out = tk.StringVar()
        self.resolve = tk.StringVar()
        self.overwrite = tk.BooleanVar()
        self.mode = tk.StringVar(value="m2")
        self.phy2_on = tk.BooleanVar()
        self.phy2_file = tk.StringVar()
        self.root_dir = tk.StringVar()
        self.prefix = tk.StringVar()

        md = tk.LabelFrame(f, text="What to read", padx=6, pady=6)
        md.pack(fill="x", pady=(0, 8))
        tk.Radiobutton(md, text=".m2 models  ->  .phys (and optionally .phy2)", variable=self.mode,
                       value="m2", command=self.mode_changed).pack(side="left")
        tk.Radiobutton(md, text="existing .phys files  ->  .phy2", variable=self.mode,
                       value="phys", command=self.mode_changed).pack(side="left", padx=16)

        box = tk.LabelFrame(f, text="Input", padx=6, pady=6)
        box.pack(fill="x")
        row = tk.Frame(box)
        row.pack(fill="x")
        tk.Button(row, text="Add file(s)...", command=self.add_files).pack(side="left")
        tk.Button(row, text="Add folder...", command=self.add_folder).pack(side="left", padx=6)
        tk.Button(row, text="Remove selected", command=self.remove_sel).pack(side="left")
        tk.Button(row, text="Clear", command=lambda: self.lst.delete(0, "end")).pack(side="left", padx=6)
        lf = tk.Frame(box)
        lf.pack(fill="x", pady=(6, 0))
        self.lst = tk.Listbox(lf, height=6, selectmode="extended")
        sb = tk.Scrollbar(lf, command=self.lst.yview)
        self.lst.configure(yscrollcommand=sb.set)
        self.lst.pack(side="left", fill="x", expand=True)
        sb.pack(side="left", fill="y")

        opt = tk.LabelFrame(f, text="Output", padx=6, pady=6)
        opt.pack(fill="x", pady=8)
        opt.columnconfigure(1, weight=1)
        self.path_row(opt, 0, ".phys folder:", self.out, "empty = next to each model")
        self.resolve_w = self.path_row(opt, 1, "Folder of <fdid>.phys:", self.resolve,
                                       "only for models that name an external .phys")
        tk.Checkbutton(opt, text="Overwrite existing .phys", variable=self.overwrite).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        p2 = tk.LabelFrame(f, text="PHY2 (what Equip Plus reads) - optional", padx=6, pady=6)
        p2.pack(fill="x", pady=(0, 8))
        p2.columnconfigure(1, weight=1)
        self.phy2_chk = tk.Checkbutton(p2, text="Also write <model name>.phy2 next to each .phys",
                                       variable=self.phy2_on, command=self.toggle_phy2)
        self.phy2_chk.grid(row=0, column=0, columnspan=3, sticky="w")
        self.p2w = []
        self.p2w += self.path_row(p2, 1, "Combined table (optional):", self.phy2_file,
                                  "one file with every model, e.g. WXLEquipPlusPhysics.phy2", save=True)
        self.p2w += self.path_row(p2, 2, "Archive root folder:", self.root_dir, "empty = the folder you added")
        tk.Label(p2, text="Key prefix:").grid(row=3, column=0, sticky="w", pady=2)
        e = tk.Entry(p2, textvariable=self.prefix)
        e.grid(row=3, column=1, sticky="ew", padx=6)
        self.p2w.append(e)
        self.toggle_phy2()

        bar = tk.Frame(f)
        bar.pack(fill="x", pady=(0, 8))
        self.btn = tk.Button(bar, text="Extract", command=self.run)
        self.btn.pack(side="left")
        self.status = tk.Label(bar, text="")
        self.status.pack(side="left", padx=10)
        lg = tk.Frame(f)
        lg.pack(fill="both", expand=True)
        self.log = tk.Text(lg, height=14, wrap="none", state="disabled", font=("Courier New", 9))
        ls = tk.Scrollbar(lg, command=self.log.yview)
        self.log.configure(yscrollcommand=ls.set)
        self.log.pack(side="left", fill="both", expand=True)
        ls.pack(side="left", fill="y")
        root.after(100, self.pump)

    def path_row(self, parent, r, label, var, hint, save=False):
        tk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=2)
        e = tk.Entry(parent, textvariable=var)
        e.grid(row=r, column=1, sticky="ew", padx=6)
        b = tk.Button(parent, text="Browse...", command=lambda: self.pick(var, save))
        b.grid(row=r, column=2)
        tk.Label(parent, text=hint, fg="#777777").grid(row=r, column=3, sticky="w", padx=6)
        return [e, b]

    def toggle_phy2(self):
        on = self.phy2_on.get() or self.mode.get() == "phys"
        for w in self.p2w:
            w.configure(state="normal" if on else "disabled")

    def mode_changed(self):
        phys = self.mode.get() == "phys"
        self.lst.delete(0, "end")   # a list of models and a list of .phys files do not mix
        if phys:
            self.phy2_on.set(True)
        self.phy2_chk.configure(state="disabled" if phys else "normal")
        for w in self.resolve_w:
            w.configure(state="disabled" if phys else "normal")
        self.toggle_phy2()

    def pick(self, var, save=False):
        if save:
            d = filedialog.asksaveasfilename(title="PHY2 table", defaultextension=".phy2",
                                             initialfile="WXLEquipPlusPhysics.phy2",
                                             filetypes=[("PHY2 table", "*.phy2"), ("All files", "*.*")])
        else:
            d = filedialog.askdirectory()
        if d:
            var.set(os.path.normpath(d))

    def add_files(self):
        phys = self.mode.get() == "phys"
        res = filedialog.askopenfilenames(
            title="Choose .phys file(s)" if phys else "Choose .m2 model(s)",
            filetypes=[("PHYS files", "*.phys")] if phys else [("M2 models", "*.m2"), ("All files", "*.*")])
        for f in self.root.tk.splitlist(res):   # Python 2 may hand back one string, not a tuple
            self._add(os.path.normpath(f))

    def add_folder(self):
        d = filedialog.askdirectory(title="Choose a folder (every %s below it is read)" % (".phys" if self.mode.get() == "phys" else ".m2"))
        if d:
            self._add(os.path.normpath(d))

    def _add(self, p):
        if p not in self.lst.get(0, "end"):
            self.lst.insert("end", p)

    def remove_sel(self):
        for i in reversed(self.lst.curselection()):
            self.lst.delete(int(i))

    def say(self, s):
        if isinstance(s, bytes) and str is bytes:   # Python 2 byte string with non-ASCII paths
            s = s.decode("latin-1", "replace")
        self.log.configure(state="normal")
        self.log.insert("end", s)
        self.log.see("end")
        self.log.configure(state="disabled")

    def pump(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, int):
                    self.finish(item)
                else:
                    self.say(item)
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def argv(self):
        items = list(self.lst.get(0, "end"))
        if not items:
            raise ValueError("Add at least one model or folder.")
        a = items
        if self.mode.get() == "phys":
            a.append("--from-phys")
        if self.out.get().strip():
            a += ["-o", self.out.get().strip()]
        if self.resolve.get().strip():
            a += ["--resolve", self.resolve.get().strip()]
        if self.overwrite.get():
            a.append("--overwrite")
        if self.phy2_on.get() and self.mode.get() == "m2":
            a.append("--phy2")
        if self.phy2_on.get():
            if self.phy2_file.get().strip():
                a += ["--phy2-all", self.phy2_file.get().strip()]
            if self.root_dir.get().strip():
                a += ["--root", self.root_dir.get().strip()]
            if self.prefix.get().strip():
                a += ["--key-prefix", self.prefix.get().strip()]
        return a

    def run(self):
        if self.busy:
            return
        try:
            argv = self.argv()
        except ValueError as e:
            messagebox.showwarning("Extract physics", str(e))
            return
        self.busy = True
        self.btn.configure(state="disabled")
        self.status.configure(text="Working...")
        self.say("\n")
        t = threading.Thread(target=self.work, args=(argv,))
        t.daemon = True
        t.start()

    def work(self, argv):
        old = sys.stdout
        sys.stdout = _QueueWriter(self.q)   # the window thread prints nothing, so a plain swap is safe
        try:
            rc = cli_main(argv)
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
        except Exception as e:   # keep the window alive whatever happens
            self.q.put("error: %s\n" % e)
            rc = 1
        finally:
            sys.stdout = old
        self.q.put(int(rc or 0))   # never touch Tk from this thread

    def finish(self, rc):
        self.busy = False
        self.btn.configure(state="normal")
        self.status.configure(text={0: "Done.", 2: "Done, some models skipped (see log)."}.get(
            rc, "Nothing was written (see log)."))


def gui_main():
    root = tk.Tk()
    root.title("Extract physics from .m2")
    root.geometry("820x740")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(cli_main())
    if tk is None:
        print("Tkinter is not available; run with arguments instead (python m2_extract_phys_py26.py --help)")
        sys.exit(1)
    gui_main()
