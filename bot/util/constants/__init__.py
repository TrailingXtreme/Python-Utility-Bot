"""
bot/util/constants/__init__.py
───────────────────────────────
Static bot constants: metadata, emoji IDs, colour codes, reply strings.

What was REMOVED vs the nextcord version (and where it moved):
  Database  → asyncpg pool on bot.pool (Phase 4)
  Tokens    → settings.secret_id / settings.topgg_token  (bot/config.py)
  Algolia   → settings.algolia_app_id / .algolia_api_key  (bot/config.py)
  Spotify   → settings.spotify_client_id / .secret        (bot/config.py)
  RapidApi  → settings.joke_api                           (bot/config.py)
  PasteBin  → not used anywhere; dropped entirely

What was CHANGED:
  nextcord.Permissions → discord.Permissions
  Client.token / .debug / .version → use settings (bot/config.py)
  NamedTuple misuse → plain class with class-level annotations
"""

import discord

__all__ = [
    "Activities",
    "Client",
    "Colours",
    "Emojis",
    "Icons",
    "RedirectOutput",
    "Source",
    "ERROR_REPLIES",
    "NEGATIVE_REPLIES",
    "POSITIVE_REPLIES",
]


# ── Discord VC Activities ─────────────────────────────────────────────────────
# Tuple layout: (application_id, emoji, category_flag, description)
#   category_flag: 0 = casual / watch, 1 = board or card game

class Activities:
    """
    Metadata for Discord voice-channel Activities (VC games / Watch Together).
    Access the full mapping via Activities._ACTIVITIES.
    """
    _ACTIVITIES: dict[str, tuple[int, str, int, str]] = {
        "Watch Together":       (880218394199220334, "📺", 0, "Watch YouTube videos together."),
        "Sketch Heads":         (902271654783242291, "✏️", 0, "Draw and guess, skribbl.io-style."),
        "Word Snacks":          (879863976006127627, "🔤", 0, "Multiplayer word-search game."),
        "Chess in the Park":    (832012774040141894, "♟️", 1, "Classic chess with friends."),
        "Checkers in the Park": (832013003968348200, "🔴", 1, "Checkers, but more kings."),
        "Letter League":        (879863686565621790, "📝", 1, "Crossword-style word game."),
        "Putt Party":           (945737671223947305, "⛳", 0, "Mini-golf mayhem."),
        "Blazing 8s":           (832025144389533716, "🃏", 1, "Crazy Eights-style card game."),
    }


# ── Bot metadata ──────────────────────────────────────────────────────────────

class Client:
    """
    Static bot metadata.  Environment-derived values (token, debug flag,
    git SHA) live in bot/config.py — import `settings` for those.
    """
    name:            str = "Discord Utility Bot"
    default_prefix:  str = "t!"
    guild_id:        int = 932264473408966656   # primary / dev guild
    test_guild_id:   int = 866235308416040971
    bot_version:     str = "5.0.0"
    github_bot_repo: str = "https://github.com/abindent/Nextcord-Utility-Bot"

    invite_permissions: discord.Permissions = discord.Permissions(
        view_channel=True,
        send_messages=True,
        send_messages_in_threads=True,
        manage_messages=True,
        manage_threads=True,
        embed_links=True,
        attach_files=True,
        read_message_history=True,
        add_reactions=True,
        use_external_emojis=True,
        change_nickname=True,
        create_public_threads=True,
        create_private_threads=True,
        view_audit_log=True,
    )


# ── Emoji IDs ─────────────────────────────────────────────────────────────────
# All IDs reference emojis from the bot's home server.
# Update these if you host the bot on a different server.

class Emojis:
    # ── General ──────────────────────────────────────────────────────────────
    boxing_glove = "\U0001F94A"
    cross_mark = "\u274C"
    game_die = "\U0001F3B2"
    sunny = "\u2600\ufe0f"
    star = "\u2B50"
    christmas_tree = "\U0001F384"
    check = "\u2611"
    envelope = "\U0001F4E8"
    gear = ":gear:"
    trashcan = "<:dustbin:949602736633167882>"
    ok_hand = ":ok_hand:"
    hand_raised = "\U0001F64B"
    upload = "\U0001f4dd"
    snekbox = "\U0001f40d"
    member_join = "<:member_join:942985122846752798>"
    repeat = "🔁"
    warning = "\u26A0\uFE0F"

    # ── Giveaway ─────────────────────────────────────────────────────────────
    tada = "\U0001f389"
    animated_tada = "<a:tada2:968745311327649793>"

    # ── Top.gg ───────────────────────────────────────────────────────────────
    topggemoji = "<:topgg:971639606099464264>"

    # ── Pagination ───────────────────────────────────────────────────────────
    FIRST_EMOJI = "\u23EE"   # ⏮
    LEFT_EMOJI = "\u2B05"   # ⬅
    RIGHT_EMOJI = "\u27A1"   # ➡
    LAST_EMOJI = "\u23ED"   # ⏭

    # ── Status ───────────────────────────────────────────────────────────────
    confirmation = "\u2705"
    decline = "\u274c"
    x = "\U0001f1fd"
    o = "\U0001f1f4"

    # ── Music player ─────────────────────────────────────────────────────────
    resume = "<:emoji_1:900445170103889980>"
    pause = "<:emoji_2:900445202899140648>"
    loop = "<:emoji_7:900445329982369802>"
    closeConnection = "<a:closeimout:848156958834032650>"
    mute = "<:muted:978168504882716702>"
    halfvolume = "<:halfvolume:978168377069674526>"
    fullvolume = "<:fullvolume:978168177005576283>"
    shuffle = "<:shuffle:978188396755320862>"
    list_emoji = "📜"

    # ── Number emojis (used by games) ────────────────────────────────────────
    number_emojis: dict[int, str] = {
        1: "\u0031\ufe0f\u20e3",
        2: "\u0032\ufe0f\u20e3",
        3: "\u0033\ufe0f\u20e3",
        4: "\u0034\ufe0f\u20e3",
        5: "\u0035\ufe0f\u20e3",
        6: "\u0036\ufe0f\u20e3",
        7: "\u0037\ufe0f\u20e3",
        8: "\u0038\ufe0f\u20e3",
        9: "\u0039\ufe0f\u20e3",
    }

    # ── Animated Emojis ───────────────────────────────────────────────────────────────
    # For all animated emojis, the format is <a:name:id> where "a" indicates it's animated.
    animated_yellow = "<a:yellow:1514829678374944859>"
    spinner = "<a:Spinner:1514829650218450945>"
    online = "<a:online:1514829611572396234>"
    idle = "<a:idle:1514829664324414720>"
    offline = "<a:offline:1514829609126989844>"
    joyRow = "<a:JoyRow:1514829597370482728>"
    animated_xd = "<a:Xd:1514829675543662654>"
    animated_banned = "<a:Banned:1514829491715838062>"
    hearzrainbow = "<a:herzrainbow:1514829575945977948>"
    nitro = "<a:nitro:1514825316739190804>"

    # ── Wordle letter emojis ──────────────────────────────────────────────────
    # Used by util/game/__init__.py.  Three colour sets × 26 letters = 78 IDs.
    # These are the original emoji IDs from the bot's home server.
    EMOJI_CODES: dict[str, dict[str, str]] = {
        "green": {
            "a": "<:a_green:952504070709575710>",  "b": "<:b_green:952504071737184276>",
            "c": "<:c_green:952504072823521290>",  "d": "<:d_green:952504073016475709>",
            "e": "<:e_green:952504073842749511>",  "f": "<:f_green:952504075277205514>",
            "g": "<:g_green:952504077407903744>",  "h": "<:h_green:952504078011871245>",
            "i": "<:i_green:952504079479894046>",  "j": "<:j_green:952504080171950120>",
            "k": "<:k_green:952504084571783188>",  "l": "<:l_green:952504081912582184>",
            "m": "<:m_green:952504086396276787>",  "n": "<:n_green:952504084408193044>",
            "o": "<:o_green:952504085465141268>",  "p": "<:p_green:952504085507080213>",
            "q": "<:q_green:952504087272890378>",  "r": "<:r_green:952504087851728966>",
            "s": "<:s_green:952504311798169600>",  "t": "<:t_green:952504312498651137>",
            "u": "<:u_green:952504312922255380>",  "v": "<:v_green:952504313761107988>",
            "w": "<:w_green:952504314843250698>",  "x": "<:x_green:952504315799543859>",
            "y": "<:y_green:952504316839755816>",  "z": "<:z_green:952504317770883132>",
        },
        "yellow": {
            "a": "<:a_yellow:952504348628377650>", "b": "<:b_yellow:952504349962170378>",
            "c": "<:c_yellow:952504351002341376>", "d": "<:d_yellow:952504351421775872>",
            "e": "<:e_yellow:952504352239669308>", "f": "<:f_yellow:952504352575209513>",
            "g": "<:g_yellow:952504355418955776>", "h": "<:h_yellow:952504357121830912>",
            "i": "<:i_yellow:952504358048759808>", "j": "<:j_yellow:952504363253899294>",
            "k": "<:k_yellow:952504363220348938>", "l": "<:l_yellow:952504359835541504>",
            "m": "<:m_yellow:952504366668087336>", "n": "<:n_yellow:952504363937579028>",
            "o": "<:o_yellow:952504365028085840>", "p": "<:p_yellow:952504365996974120>",
            "q": "<:q_yellow:952504368240922635>", "r": "<:r_yellow:952504368853319710>",
            "s": "<:s_yellow:952504369453080576>", "t": "<:t_yellow:952504370107396096>",
            "u": "<:u_yellow:952504370753318983>", "v": "<:v_yellow:952504371327946802>",
            "w": "<:w_yellow:952504372460408853>", "x": "<:x_yellow:952504373785813052>",
            "y": "<:y_yellow:952504374876332102>", "z": "<:z_yellow:952504375203491842>",
        },
        "gray": {
            "a": "<:a_grey:952503991336574976>",   "b": "<:b_grey:952503992385175572>",
            "c": "<:c_grey:952503993110777876>",   "d": "<:d_grey:952503993752498176>",
            "e": "<:e_grey:952503994205495306>",   "f": "<:f_grey:952503994582966312>",
            "g": "<:g_grey:952503997011492904>",   "h": "<:h_grey:952503998940860436>",
            "i": "<:i_grey:952504000002031658>",   "j": "<:j_grey:952504000824090688>",
            "k": "<:k_grey:952504006217986108>",   "l": "<:l_grey:952504003080650832>",
            "m": "<:m_grey:952504004896784455>",   "n": "<:n_grey:952504005978906644>",
            "o": "<:o_grey:952504006612250674>",   "p": "<:p_grey:952504007547564082>",
            "q": "<:q_grey:952504008860377178>",   "r": "<:r_grey:952504009653108806>",
            "s": "<:s_grey:952504009799917570>",   "t": "<:t_grey:952504010626195487>",
            "u": "<:u_grey:952504011196616714>",   "v": "<:v_grey:952504011779616768>",
            "w": "<:w_grey:952504012358451243>",   "x": "<:x_grey:952504012954013697>",
            "y": "<:y_grey:952504013474123816>",   "z": "<:z_grey:952504013742571530>",
        },
    }

    # ── Social ───────────────────────────────────────────────────────────────
    discord_emoji = "<:discord:1514829525668855978>"
    youtube_emoji = "<a:yt:1514827814736629819>"
    github_emoji = "<:github:1514827777906446489>"


# ── Icon URLs ─────────────────────────────────────────────────────────────────

class Icons:
    questionmark = "https://cdn.discordapp.com/emojis/512367613339369475.png"
    bookmark = (
        "https://images-ext-2.discordapp.net/external/"
        "zl4oDwcmxUILY7sD9ZWE2fU5R7n6QcxEmPYSE5eddbg/"
        "%3Fv%3D1/https/cdn.discordapp.com/emojis/654080405988966419.png"
        "?width=20&height=20"
    )


# ── Colour palette ────────────────────────────────────────────────────────────

class Colours:
    """
    HTML5 named colour codes as hex strings.
    Used wherever a random or named colour is needed for embeds.
    """
    HTML5_COLOR_CODES: dict[str, str] = {
        "aliceblue": "0xf0f8ff", "antiquewhite": "0xfaebd7", "aqua": "0x00ffff",
        "aquamarine": "0x7fffd4", "azure": "0xf0ffff", "beige": "0xf5f5dc",
        "bisque": "0xffe4c4", "black": "0x000000", "blanchedalmond": "0xffebcd",
        "blue": "0x0000ff", "blueviolet": "0x8a2be2", "brown": "0xa52a2a",
        "burlywood": "0xdeb887", "cadetblue": "0x5f9ea0", "chartreuse": "0x7fff00",
        "chocolate": "0xd2691e", "coral": "0xff7f50", "cornflowerblue": "0x6495ed",
        "cornsilk": "0xfff8dc", "crimson": "0xdc143c", "cyan": "0x00ffff",
        "darkblue": "0x00008b", "darkcyan": "0x008b8b", "darkgoldenrod": "0xb8860b",
        "darkgray": "0xa9a9a9", "darkgreen": "0x006400", "darkkhaki": "0xbdb76b",
        "darkmagenta": "0x8b008b", "darkolivegreen": "0x556b2f", "darkorange": "0xff8c00",
        "darkorchid": "0x9932cc", "darkred": "0x8b0000", "darksalmon": "0xe9967a",
        "darkseagreen": "0x8fbc8f", "darkslateblue": "0x483d8b", "darkslategray": "0x2f4f4f",
        "darkturquoise": "0x00ced1", "darkviolet": "0x9400d3", "deeppink": "0xff1493",
        "deepskyblue": "0x00bfff", "dimgray": "0x696969", "dodgerblue": "0x1e90ff",
        "firebrick": "0xb22222", "floralwhite": "0xfffaf0", "forestgreen": "0x228b22",
        "fuchsia": "0xff00ff", "gainsboro": "0xdcdcdc", "ghostwhite": "0xf8f8ff",
        "gold": "0xffd700", "goldenrod": "0xdaa520", "gray": "0x808080",
        "green": "0x008000", "greenyellow": "0xadff2f", "honeydew": "0xf0fff0",
        "hotpink": "0xff69b4", "indianred": "0xcd5c5c", "indigo": "0x4b0082",
        "ivory": "0xfffff0", "khaki": "0xf0e68c", "lavender": "0xe6e6fa",
        "lavenderblush": "0xfff0f5", "lawngreen": "0x7cfc00", "lemonchiffon": "0xfffacd",
        "lightblue": "0xadd8e6", "lightcoral": "0xf08080", "lightcyan": "0xe0ffff",
        "lightgoldenrodyellow": "0xfafad2", "lightgray": "0xd3d3d3", "lightgreen": "0x90ee90",
        "lightpink": "0xffb6c1", "lightsalmon": "0xffa07a", "lightseagreen": "0x20b2aa",
        "lightskyblue": "0x87cefa", "lightslategray": "0x778899", "lightsteelblue": "0xb0c4de",
        "lightyellow": "0xffffe0", "lime": "0x00ff00", "limegreen": "0x32cd32",
        "linen": "0xfaf0e6", "magenta": "0xff00ff", "maroon": "0x800000",
        "mediumaquamarine": "0x66cdaa", "mediumblue": "0x0000cd", "mediumorchid": "0xba55d3",
        "mediumpurple": "0x9370db", "mediumseagreen": "0x3cb371", "mediumslateblue": "0x7b68ee",
        "mediumspringgreen": "0x00fa9a", "mediumturquoise": "0x48d1cc",
        "mediumvioletred": "0xc71585", "midnightblue": "0x191970", "mintcream": "0xf5fffa",
        "mistyrose": "0xffe4e1", "moccasin": "0xffe4b5", "navajowhite": "0xffdead",
        "navy": "0x000080", "oldlace": "0xfdf5e6", "olive": "0x808000",
        "olivedrab": "0x6b8e23", "orange": "0xffa500", "orangered": "0xff4500",
        "orchid": "0xda70d6", "palegoldenrod": "0xeee8aa", "palegreen": "0x98fb98",
        "paleturquoise": "0xafeeee", "palevioletred": "0xdb7093", "papayawhip": "0xffefd5",
        "peachpuff": "0xffdab9", "peru": "0xcd853f", "pink": "0xffc0cb",
        "plum": "0xdda0dd", "powderblue": "0xb0e0e6", "purple": "0x800080",
        "red": "0xff0000", "rosybrown": "0xbc8f8f", "royalblue": "0x4169e1",
        "saddlebrown": "0x8b4513", "salmon": "0xfa8072", "sandybrown": "0xf4a460",
        "seagreen": "0x2e8b57", "seashell": "0xfff5ee", "sienna": "0xa0522d",
        "silver": "0xc0c0c0", "skyblue": "0x87ceeb", "slateblue": "0x6a5acd",
        "slategray": "0x708090", "snow": "0xfffafa", "springgreen": "0x00ff7f",
        "steelblue": "0x4682b4", "tan": "0xd2b48c", "teal": "0x008080",
        "thistle": "0xd8bfd8", "tomato": "0xff6347", "turquoise": "0x40e0d0",
        "violet": "0xee82ee", "wheat": "0xf5deb3", "white": "0xffffff",
        "whitesmoke": "0xf5f5f5", "yellow": "0xffff00", "yellowgreen": "0x9acd32",
    }


# ── Misc ──────────────────────────────────────────────────────────────────────

class RedirectOutput:
    delete_delay: int = 10


class Source:
    github:        str = Client.github_bot_repo
    github_avatar: str = "https://avatars1.githubusercontent.com/u/9919"


# ── Bot reply strings ─────────────────────────────────────────────────────────
# Used throughout cogs for randomised bot responses.

ERROR_REPLIES: list[str] = [
    "Please don't do that.",
    "You have to stop.",
    "Do you mind?",
    "In the future, don't do that.",
    "That was a mistake.",
    "You blew it.",
    "You're bad at computers.",
    "Are you trying to kill me?",
    "Noooooo!!",
    "I can't believe you've done this.",
]

NEGATIVE_REPLIES: list[str] = [
    "Noooooo!!",
    "Nope.",
    "I'm sorry Dave, I'm afraid I can't do that.",
    "I don't think so.",
    "Not gonna happen.",
    "Out of the question.",
    "Huh? No.",
    "Nah.",
    "Not likely.",
    "No way, José.",
    "Not in a million years.",
    "Fat chance.",
    "Certainly not.",
    "NEGATORY.",
    "Nuh-uh.",
    "Not in my house!",
]

POSITIVE_REPLIES: list[str] = [
    "Yep.",
    "Absolutely!",
    "Can do!",
    "Affirmative!",
    "Yeah okay.",
    "Sure.",
    "Sure thing!",
    "You're the boss!",
    "Okay.",
    "No problem.",
    "I got you.",
    "Alright.",
    "You got it!",
    "ROGER THAT",
    "Of course!",
    "Aye aye, cap'n!",
    "I'll allow it.",
]
