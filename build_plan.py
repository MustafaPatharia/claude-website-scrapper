#!/usr/bin/env python3
"""Master rebuild allocator: combine video catalog + scraped section metadata +
images into ONE plan that (a) uses each video at most once, (b) places every
image, (c) varies animation per section. Emits _rebuild-plan.json + .md.

Deterministic — no randomness (greedy keyword scoring). Re-run anytime.
"""
import json, glob, os, re
from collections import Counter

PAGES = sorted(glob.glob("output/pages/*.json"))
DROP_VIDEOS = {"running-track-aerial-flyover.mp4"}          # off-brand (CLAUDE.md)
RIGHTS_FLAG = {"dubai-google-earth-zoom.mp4"}               # verify before public use

# --- load videos ---
vraw = json.load(open("Trot Videos/_videos.json"))
clips = vraw if isinstance(vraw, list) else next(v for v in vraw.values() if isinstance(v, list))
videos = []
for c in clips:
    f = c["file"]
    if f in DROP_VIDEOS:
        continue
    bf = c.get("best_for", "")
    bf = " ".join(bf) if isinstance(bf, list) else str(bf)
    tags = c.get("tags", [])
    tags = " ".join(tags) if isinstance(tags, list) else str(tags)
    videos.append({
        "file": f, "loop": bool(c.get("loop_friendly")),
        "text": " ".join([bf, tags, c.get("mood", ""), c.get("description", "")]).lower(),
        "rights": f in RIGHTS_FLAG,
    })

# --- animation primitives, chosen by role (rotate for variety) ---
ANIM_BY_ROLE = {
    "hero / top banner":     ["HeroParticles", "BackgroundVideo+VideoScrub", "ParallaxMedia"],
    "about":                 ["Reveal(blur)", "ParallaxMedia", "KineticHeading"],
    "services":              ["VideoGrid", "HorizontalScroll", "Reveal(up)+stagger"],
    "features":              ["HorizontalScroll", "VideoGrid", "Reveal(scale)"],
    "portfolio":             ["VideoGrid", "HorizontalScroll", "ParallaxMedia"],
    "team":                  ["BackgroundVideo strip", "Reveal(left)", "Marquee"],
    "testimonial":           ["Reveal(up)", "Marquee", "KineticHeading"],
    "pricing":               ["Reveal(scale)+stagger", "Counter", "Reveal(up)"],
    "FAQ":                   ["Reveal(up) <details>", "Reveal(left)"],
    "contact":               ["Reveal(up)", "ParallaxMedia"],
    "footer":                ["Reveal(up)", "Marquee"],
    "gallery / logo strip":  ["Marquee", "VideoGrid", "Reveal(scale)"],
    "content / editorial":   ["Reveal(up)", "ParallaxMedia", "ScrubBand"],
    "generic content band":  ["Reveal(up)", "Reveal(left)", "Reveal(right)", "ScrubBand", "Counter"],
}
# role -> video keyword hints (to bias video->section matching)
ROLE_VIDEO_HINTS = {
    "hero / top banner": "hero establishing full-bleed",
    "about": "about mission section divider intro",
    "services": "services scroll-scrub section",
    "features": "feature technology scroll-driven",
    "team": "team operations crew banner",
    "content / editorial": "section background overlay",
    "gallery / logo strip": "grid gallery tile loop",
    "generic content band": "section background text overlay loop",
}


def norm_role(r):
    r = (r or "").lower()
    for key in ANIM_BY_ROLE:
        if key.split(" /")[0].split(" ")[0] in r:
            return key
    return "generic content band"


def extract_role(ai_desc):
    m = re.search(r"likely role:\s*([^.]+)\.", ai_desc or "", re.I)
    return norm_role(m.group(1)) if m else "generic content band"


# --- collect all sections across pages ---
sections = []
img_universe = set(os.path.splitext(f)[0].split("_", 1)[-1] for f in os.listdir("output/images"))
all_disk_images = set(os.listdir("output/images"))
for pf in PAGES:
    d = json.load(open(pf))
    slug = os.path.basename(pf).replace(".html.json", "").replace(".json", "")
    if slug == "index":       # dedupe homepage (index == index.html)
        continue
    for s in d["sections"]:
        role = extract_role(s.get("ai_agent_description", ""))
        sections.append({
            "page": slug, "idx": s["index"], "title": s["title"],
            "role": role, "images": s.get("images", []),
            "img_count": s["image_count"],
            "ai": s.get("ai_agent_description", ""),
            "video": None, "anim": None,
        })

# --- assign videos: greedy, one per section, highest-value roles first ---
ROLE_PRIORITY = {"hero / top banner": 0, "features": 1, "services": 1, "team": 2,
                 "content / editorial": 3, "gallery / logo strip": 3,
                 "about": 3, "generic content band": 4}
video_targets = sorted(
    [s for s in sections if s["role"] in ("hero / top banner", "features", "services",
                                          "team", "content / editorial", "gallery / logo strip",
                                          "about")],
    key=lambda s: (ROLE_PRIORITY.get(s["role"], 9), s["page"], s["idx"]))

used = set()
for s in video_targets:
    if len(used) >= len(videos):
        break
    hint = ROLE_VIDEO_HINTS.get(s["role"], "") + " " + s["ai"].lower()
    words = set(re.findall(r"[a-z]+", hint))
    best, best_score = None, -1
    for v in videos:
        if v["file"] in used:
            continue
        score = sum(1 for w in words if len(w) > 3 and w in v["text"])
        # prefer non-loop for hero/feature, loop for background bands
        if s["role"] in ("content / editorial", "generic content band", "gallery / logo strip") and v["loop"]:
            score += 2
        if s["role"] == "hero / top banner" and not v["loop"]:
            score += 1
        if score > best_score:
            best, best_score = v, score
    if best:
        s["video"] = best["file"]
        s["video_rights_flag"] = best["rights"]
        used.add(best["file"])

# --- assign animation (rotate within role for variety) ---
role_counter = Counter()
for s in sections:
    opts = ANIM_BY_ROLE.get(s["role"], ANIM_BY_ROLE["generic content band"])
    s["anim"] = opts[role_counter[s["role"]] % len(opts)]
    role_counter[s["role"]] += 1

# --- image coverage audit (source sections already reference their own images) ---
placed = set()
for s in sections:
    placed.update(s["images"])
orphans = sorted(all_disk_images - placed)
# place orphans into image-friendly sections with fewest images
if orphans:
    hosts = sorted([s for s in sections if not s["video"]], key=lambda s: len(s["images"]))
    for i, img in enumerate(orphans):
        hosts[i % len(hosts)]["images"].append(img)
        hosts[i % len(hosts)].setdefault("added_images", []).append(img)
    placed.update(orphans)

# --- write plan ---
plan = {
    "pages": sorted(set(s["page"] for s in sections)),
    "section_count": len(sections),
    "videos_available": len(videos),
    "videos_used": len(used),
    "videos_dropped": sorted(DROP_VIDEOS),
    "videos_unused": sorted(v["file"] for v in videos if v["file"] not in used),
    "images_total": len(all_disk_images),
    "images_placed": len(placed & all_disk_images),
    "image_orphans_after": sorted(all_disk_images - placed),
    "sections": sections,
}
json.dump(plan, open("_rebuild-plan.json", "w"), indent=2, ensure_ascii=False)

# audit: each used video exactly once
vcount = Counter(s["video"] for s in sections if s["video"])
dupes = {k: c for k, c in vcount.items() if c > 1}

# --- readable md ---
L = ["# Promax Global — Rebuild Plan (auto-generated by build_plan.py)\n",
     f"- Pages: {len(plan['pages'])}  | Sections: {plan['section_count']}",
     f"- Videos: {plan['videos_used']}/{plan['videos_available']} used once each "
     f"(dropped {', '.join(plan['videos_dropped'])}); "
     f"unused: {', '.join(plan['videos_unused']) or 'none'}",
     f"- Video reuse violations: {dupes or 'NONE ✅'}",
     f"- Images: {plan['images_placed']}/{plan['images_total']} placed; "
     f"orphans: {plan['image_orphans_after'] or 'NONE ✅'}\n"]
cur = None
for s in sorted(sections, key=lambda s: (s["page"], s["idx"])):
    if s["page"] != cur:
        cur = s["page"]
        L.append(f"\n## {cur}")
    vid = f" 🎬 {s['video']}" + (" ⚠️rights" if s.get("video_rights_flag") else "") if s["video"] else ""
    imgs = f" 🖼️{len(s['images'])}" if s["images"] else ""
    L.append(f"- [{s['idx']}] **{s['title'][:44]}** · _{s['role']}_ · ▶ {s['anim']}{vid}{imgs}")
open("_rebuild-plan.md", "w").write("\n".join(L))

print(f"videos used {len(used)}/{len(videos)} | dupes={dupes or 'none'} | "
      f"images {len(placed & all_disk_images)}/{len(all_disk_images)} | "
      f"orphans={len(all_disk_images - placed)}")
print("-> _rebuild-plan.json + _rebuild-plan.md")
