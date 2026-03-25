import re

EVENT_KEYWORDS = [
    "community day",
    "raid day",
    "raid hour",
    "raid weekend",
    "raid battle",
    "spotlight hour",
    "gigantamax",
    "ultra unlock",
    "go fest",
    "research day",
    "limited research",
    "special research",
    "elite raid",
    "mega raid",
    "remote raid",
    "incense day",
    "adventure week",
    "festival of",
    "buddy event",
    "hatching event",
    "season of",
    "event begins",
    "event ends",
    "will be appearing",
    "will be available",
    "will be hatching",
    "will be featured",
    "featured attack",
    "catch bonus",
    r"2[x×] catch",
    r"3[x×] catch",
    r"2[x×] stardust",
    r"3[x×] stardust",
    r"2[x×] xp",
    r"3[x×] xp",
    r"\?\?\?",
]

EXCLUDE_KEYWORDS = [
    "merchandise",
    "merch",
    "shop now",
    "official store",
    "buy now",
    "plush",
    "t-shirt",
    "figure",
    "pokémon center",
    "on sale",
    "#gofest2026",
    "#gomemories",
    "#sweepstakes",
]


def is_event_post(post):
    text = post["text"].lower()

    if any(kw.lower() in text for kw in EXCLUDE_KEYWORDS):
        return False

    for kw in EVENT_KEYWORDS:
        if re.search(kw, text, re.IGNORECASE):
            return True

    if post.get("images"):
        return True

    return False


def filter_posts(posts):
    filtered = [p for p in posts if is_event_post(p)]
    dropped  = len(posts) - len(filtered)
    print(f"  [filter] Kept {len(filtered)}, dropped {dropped} of {len(posts)} posts")
    return filtered
