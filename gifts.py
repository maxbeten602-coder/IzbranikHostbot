"""Каталог подарков (сгенерирован из web/gifts.js). name -> (value, nft, slug)."""
from __future__ import annotations

GIFTS: dict[str, tuple[float, bool, str]] = {
    'Toy Bear': (15, False, 'toybear'),
    'Eternal Rose': (25, False, 'eternalrose'),
    'Homemade Cake': (50, False, 'homemadecake'),
    'Berry Box': (50, False, 'berrybox'),
    'Cookie Heart': (50, False, 'cookieheart'),
    'B-Day Candle': (50, False, 'bdaycandle'),
    'Love Candle': (50, False, 'lovecandle'),
    'Desk Calendar': (50, False, 'deskcalendar'),
    'Candy Cane': (500, True, 'candycane'),
    'Ice Cream': (505, True, 'icecream'),
    'Top Hat': (530, True, 'tophat'),
    'Hypno Lollipop': (544, True, 'hypnolollipop'),
    'Lunar Snake': (549, True, 'lunarsnake'),
    'Jester Hat': (550, True, 'jesterhat'),
    'Party Sparkler': (587, True, 'partysparkler'),
    'Snow Mittens': (500, True, 'snowmittens'),
    'Jack-in-the-Box': (500, True, 'jackinthebox'),
    'Spy Agaric': (814, True, 'spyagaric'),
    'Kissed Frog': (721, True, 'kissedfrog'),
    'Jelly Bunny': (721, True, 'jellybunny'),
    'Trapped Heart': (690, True, 'trappedheart'),
    'Scared Cat': (721, True, 'scaredcat'),
    'Magic Potion': (600, True, 'magicpotion'),
    'Genie Lamp': (650, True, 'genielamp'),
    'Voodoo Doll': (655, True, 'voodoodoll'),
    'Crystal Ball': (666, True, 'crystalball'),
    'Flying Broom': (650, True, 'flyingbroom'),
    'Witch Hat': (550, True, 'witchhat'),
    'Santa Hat': (500, True, 'santahat'),
    'Precious Peach': (900, True, 'preciouspeach'),
    'Plush Pepe': (900, True, 'plushpepe'),
    "Durov's Cap": (1000, True, 'durovscap'),
    'Perfume Bottle': (710, True, 'perfumebottle'),
    'Vintage Cigar': (700, True, 'vintagecigar'),
    'Skull Flower': (600, True, 'skullflower'),
    'Evil Eye': (550, True, 'evileye'),
    'Hex Pot': (550, True, 'hexpot'),
    'Sharp Tongue': (600, True, 'sharptongue'),
    'Signet Ring': (700, True, 'signetring'),
    'Spiced Wine': (500, True, 'spicedwine'),
    'Bunny Muffin': (510, True, 'bunnymuffin'),
    'Astral Shard': (800, True, 'astralshard'),
    'Hanging TON': (505, True, 'hangingstar'),
}

SLUG_TO_NAME: dict[str, str] = {slug: name for name, (_, _, slug) in GIFTS.items()}
NAMES: list[str] = list(GIFTS.keys())


def find_gift(query: str) -> str | None:
    """Находит подарок по названию без учёта регистра/пробелов/знаков."""
    q = "".join(ch for ch in (query or "").lower() if ch.isalnum())
    for n in GIFTS:
        if "".join(ch for ch in n.lower() if ch.isalnum()) == q:
            return n
    return None
