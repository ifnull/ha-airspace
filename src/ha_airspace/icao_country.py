"""ICAO 24-bit address → country (ISO 3166-1 alpha-2) + flag emoji.

ICAO allocates 24-bit aircraft addresses to states in blocks (Annex 10 vol III).
This maps a hex address to its country of registration and a flag emoji, purely
for display ("🇺🇸 SWA4779"). The ranges are the canonical dump1090 / tar1090
table; a handful of UK sub-territories (Bermuda, Cayman, Isle of Man, …) and
Hong Kong sit *inside* a broader block, so those specific ranges are listed
before their catch-all and a first-match scan resolves them correctly.

Pure + cached: ``country_for`` memoizes on the hex (stable per aircraft), so the
~200-entry scan runs once per address even though payloads recompute per publish.
"""

from __future__ import annotations

from functools import lru_cache

# (start, end, iso2) inclusive. ORDER MATTERS — specific sub-ranges must precede
# their catch-all (UK territories before gb; Hong Kong before cn) for first-match.
_RANGES: tuple[tuple[int, int, str], ...] = (
    (0x004000, 0x0047FF, "zw"),
    (0x006000, 0x006FFF, "mz"),
    (0x008000, 0x00FFFF, "za"),
    (0x010000, 0x017FFF, "eg"),
    (0x018000, 0x01FFFF, "ly"),
    (0x020000, 0x027FFF, "ma"),
    (0x028000, 0x02FFFF, "tn"),
    (0x030000, 0x0307FF, "bw"),
    (0x032000, 0x032FFF, "bi"),
    (0x034000, 0x034FFF, "cm"),
    (0x035000, 0x0357FF, "km"),
    (0x036000, 0x036FFF, "cg"),
    (0x038000, 0x038FFF, "ci"),
    (0x03E000, 0x03EFFF, "ga"),
    (0x040000, 0x040FFF, "et"),
    (0x042000, 0x042FFF, "gq"),
    (0x044000, 0x044FFF, "gh"),
    (0x046000, 0x046FFF, "gn"),
    (0x048000, 0x0487FF, "gw"),
    (0x04A000, 0x04A7FF, "ls"),
    (0x04C000, 0x04CFFF, "ke"),
    (0x050000, 0x050FFF, "lr"),
    (0x054000, 0x054FFF, "mg"),
    (0x058000, 0x058FFF, "mw"),
    (0x05A000, 0x05A7FF, "mv"),
    (0x05C000, 0x05CFFF, "ml"),
    (0x05E000, 0x05E7FF, "mr"),
    (0x060000, 0x0607FF, "mu"),
    (0x062000, 0x062FFF, "ne"),
    (0x064000, 0x064FFF, "ng"),
    (0x068000, 0x068FFF, "ug"),
    (0x06A000, 0x06AFFF, "qa"),
    (0x06C000, 0x06CFFF, "cf"),
    (0x06E000, 0x06EFFF, "rw"),
    (0x070000, 0x070FFF, "sn"),
    (0x074000, 0x0747FF, "sc"),
    (0x076000, 0x0767FF, "sl"),
    (0x078000, 0x078FFF, "so"),
    (0x07A000, 0x07A7FF, "sz"),
    (0x07C000, 0x07CFFF, "sd"),
    (0x080000, 0x080FFF, "tz"),
    (0x084000, 0x084FFF, "td"),
    (0x088000, 0x088FFF, "tg"),
    (0x08A000, 0x08AFFF, "zm"),
    (0x08C000, 0x08CFFF, "cd"),
    (0x090000, 0x090FFF, "ao"),
    (0x094000, 0x0947FF, "bj"),
    (0x096000, 0x0967FF, "cv"),
    (0x098000, 0x0987FF, "dj"),
    (0x09A000, 0x09AFFF, "gm"),
    (0x09C000, 0x09CFFF, "bf"),
    (0x09E000, 0x09E7FF, "st"),
    (0x0A0000, 0x0A7FFF, "dz"),
    (0x0A8000, 0x0A8FFF, "bs"),
    (0x0AA000, 0x0AA7FF, "bb"),
    (0x0AB000, 0x0AB7FF, "bz"),
    (0x0AC000, 0x0ADFFF, "co"),
    (0x0AE000, 0x0AEFFF, "cr"),
    (0x0B0000, 0x0B0FFF, "cu"),
    (0x0B2000, 0x0B2FFF, "sv"),
    (0x0B4000, 0x0B4FFF, "gt"),
    (0x0B6000, 0x0B6FFF, "gy"),
    (0x0B8000, 0x0B8FFF, "ht"),
    (0x0BA000, 0x0BAFFF, "hn"),
    (0x0BC000, 0x0BC7FF, "vc"),
    (0x0BE000, 0x0BEFFF, "jm"),
    (0x0C0000, 0x0C0FFF, "ni"),
    (0x0C2000, 0x0C2FFF, "pa"),
    (0x0C4000, 0x0C4FFF, "do"),
    (0x0C6000, 0x0C6FFF, "tt"),
    (0x0C8000, 0x0C8FFF, "sr"),
    (0x0CA000, 0x0CA7FF, "ag"),
    (0x0CC000, 0x0CC7FF, "gd"),
    (0x0D0000, 0x0D7FFF, "mx"),
    (0x0D8000, 0x0DFFFF, "ve"),
    (0x100000, 0x1FFFFF, "ru"),
    (0x201000, 0x2017FF, "na"),
    (0x202000, 0x2027FF, "er"),
    (0x300000, 0x33FFFF, "it"),
    (0x340000, 0x37FFFF, "es"),
    (0x380000, 0x3BFFFF, "fr"),
    (0x3C0000, 0x3FFFFF, "de"),
    # --- UK + sub-territories: specifics first, gb catch-all last ---
    (0x400000, 0x4001BF, "bm"),
    (0x4001C0, 0x4001FF, "ky"),
    (0x400300, 0x4003FF, "tc"),
    (0x424135, 0x4241F2, "ky"),
    (0x424200, 0x4246FF, "bm"),
    (0x424700, 0x424899, "ky"),
    (0x424B00, 0x424BFF, "im"),
    (0x43BE00, 0x43BEFF, "bm"),
    (0x43E700, 0x43EAFD, "im"),
    (0x43EAFE, 0x43EEFF, "gg"),
    (0x400000, 0x43FFFF, "gb"),
    (0x440000, 0x447FFF, "at"),
    (0x448000, 0x44FFFF, "be"),
    (0x450000, 0x457FFF, "bg"),
    (0x458000, 0x45FFFF, "dk"),
    (0x460000, 0x467FFF, "fi"),
    (0x468000, 0x46FFFF, "gr"),
    (0x470000, 0x477FFF, "hu"),
    (0x478000, 0x47FFFF, "no"),
    (0x480000, 0x487FFF, "nl"),
    (0x488000, 0x48FFFF, "pl"),
    (0x490000, 0x497FFF, "pt"),
    (0x498000, 0x49FFFF, "cz"),
    (0x4A0000, 0x4A7FFF, "ro"),
    (0x4A8000, 0x4AFFFF, "se"),
    (0x4B0000, 0x4B7FFF, "ch"),
    (0x4B8000, 0x4BFFFF, "tr"),
    (0x4C0000, 0x4C7FFF, "rs"),
    (0x4C8000, 0x4C87FF, "cy"),
    (0x4CA000, 0x4CAFFF, "ie"),
    (0x4CC000, 0x4CCFFF, "is"),
    (0x4D0000, 0x4D07FF, "lu"),
    (0x4D2000, 0x4D27FF, "mt"),
    (0x4D4000, 0x4D47FF, "mc"),
    (0x500000, 0x5007FF, "sm"),
    (0x501000, 0x5017FF, "al"),
    (0x501800, 0x501FFF, "hr"),
    (0x502800, 0x502FFF, "lv"),
    (0x503800, 0x503FFF, "lt"),
    (0x504800, 0x504FFF, "md"),
    (0x505800, 0x505FFF, "sk"),
    (0x506800, 0x506FFF, "si"),
    (0x507800, 0x507FFF, "uz"),
    (0x508000, 0x50FFFF, "ua"),
    (0x510000, 0x5107FF, "by"),
    (0x511000, 0x5117FF, "ee"),
    (0x512000, 0x5127FF, "mk"),
    (0x513000, 0x5137FF, "ba"),
    (0x514000, 0x5147FF, "ge"),
    (0x515000, 0x5157FF, "tj"),
    (0x516000, 0x5167FF, "me"),
    (0x600000, 0x6007FF, "am"),
    (0x600800, 0x600FFF, "az"),
    (0x601000, 0x6017FF, "kg"),
    (0x601800, 0x601FFF, "tm"),
    (0x680000, 0x6807FF, "bt"),
    (0x681000, 0x6817FF, "fm"),
    (0x682000, 0x6827FF, "mn"),
    (0x683000, 0x6837FF, "kz"),
    (0x684000, 0x6847FF, "pw"),
    (0x700000, 0x700FFF, "af"),
    (0x702000, 0x702FFF, "bd"),
    (0x704000, 0x704FFF, "mm"),
    (0x706000, 0x706FFF, "kw"),
    (0x708000, 0x708FFF, "la"),
    (0x70A000, 0x70AFFF, "np"),
    (0x70C000, 0x70C7FF, "om"),
    (0x70E000, 0x70EFFF, "kh"),
    (0x710000, 0x717FFF, "sa"),
    (0x718000, 0x71FFFF, "kr"),
    (0x720000, 0x727FFF, "kp"),
    (0x728000, 0x72FFFF, "iq"),
    (0x730000, 0x737FFF, "ir"),
    (0x738000, 0x73FFFF, "il"),
    (0x740000, 0x747FFF, "jo"),
    (0x748000, 0x74FFFF, "lb"),
    (0x750000, 0x757FFF, "my"),
    (0x758000, 0x75FFFF, "ph"),
    (0x760000, 0x767FFF, "pk"),
    (0x768000, 0x76FFFF, "sg"),
    (0x770000, 0x777FFF, "lk"),
    (0x778000, 0x77FFFF, "sy"),
    (0x789000, 0x789FFF, "hk"),  # before cn
    (0x780000, 0x7BFFFF, "cn"),
    (0x7C0000, 0x7FFFFF, "au"),
    (0x800000, 0x83FFFF, "in"),
    (0x840000, 0x87FFFF, "jp"),
    (0x880000, 0x887FFF, "th"),
    (0x888000, 0x88FFFF, "vn"),
    (0x890000, 0x890FFF, "ye"),
    (0x894000, 0x894FFF, "bh"),
    (0x895000, 0x8957FF, "bn"),
    (0x896000, 0x896FFF, "ae"),
    (0x897000, 0x8977FF, "sb"),
    (0x898000, 0x898FFF, "pg"),
    (0x899000, 0x8997FF, "tw"),
    (0x8A0000, 0x8A7FFF, "id"),
    (0x900000, 0x9007FF, "mh"),
    (0x901000, 0x9017FF, "ck"),
    (0x902000, 0x9027FF, "ws"),
    (0xA00000, 0xAFFFFF, "us"),
    (0xC00000, 0xC3FFFF, "ca"),
    (0xC80000, 0xC87FFF, "nz"),
    (0xC88000, 0xC88FFF, "fj"),
    (0xC8A000, 0xC8A7FF, "nr"),
    (0xC8C000, 0xC8C7FF, "lc"),
    (0xC8D000, 0xC8D7FF, "to"),
    (0xC8E000, 0xC8E7FF, "ki"),
    (0xC90000, 0xC907FF, "vu"),
    (0xC91000, 0xC917FF, "ad"),
    (0xC92000, 0xC927FF, "dm"),
    (0xC93000, 0xC937FF, "kn"),
    (0xC94000, 0xC947FF, "ss"),
    (0xC95000, 0xC957FF, "tl"),
    (0xC97000, 0xC977FF, "tv"),
    (0xE00000, 0xE3FFFF, "ar"),
    (0xE40000, 0xE7FFFF, "br"),
    (0xE80000, 0xE80FFF, "cl"),
    (0xE84000, 0xE84FFF, "ec"),
    (0xE88000, 0xE88FFF, "py"),
    (0xE8C000, 0xE8CFFF, "pe"),
    (0xE90000, 0xE90FFF, "uy"),
    (0xE94000, 0xE94FFF, "bo"),
)

_REGIONAL_INDICATOR_A = 0x1F1E6  # 🇦; flag = two regional-indicator letters


@lru_cache(maxsize=16384)
def country_for(hex_code: str | None) -> str | None:
    """ISO 3166-1 alpha-2 country of registration for an ICAO hex, or ``None``
    if unallocated / unparseable. A leading ``~`` (non-ICAO TIS-B/ADS-R) is
    stripped before lookup. Cached on the hex."""
    if not hex_code:
        return None
    try:
        addr = int(hex_code.lstrip("~").strip(), 16)
    except ValueError:
        return None
    for start, end, iso2 in _RANGES:
        if start <= addr <= end:
            return iso2
    return None


def flag_emoji(iso2: str) -> str:
    """Render an ISO 3166-1 alpha-2 code as a regional-indicator flag emoji."""
    return "".join(chr(_REGIONAL_INDICATOR_A + ord(c) - ord("A")) for c in iso2.upper())


def flag_for(hex_code: str | None) -> str | None:
    """Flag emoji for an ICAO hex's country of registration, or ``None``."""
    iso2 = country_for(hex_code)
    return flag_emoji(iso2) if iso2 else None


__all__ = ["country_for", "flag_emoji", "flag_for"]
