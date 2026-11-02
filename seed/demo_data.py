"""Demo master data for the FloorVoice ERPNext company.

`aliases` are spoken forms (English and Tamil-script transliterations) stored in the
custom field `voice_aliases` and used both for ASR keyword biasing and fuzzy search.
"""

from __future__ import annotations

COMPANY = "FloorVoice Demo Works"
ABBR = "FVD"
COUNTRY = "Sri Lanka"
CURRENCY = "LKR"
TIMEZONE = "Asia/Colombo"
FY_START = "2026-04-01"
FY_END = "2027-03-31"

BOT_USER = "floorvoice-bot@example.com"
BOT_ROLES = [
    "Stock User",
    "Stock Manager",
    "Manufacturing User",
    "Manufacturing Manager",
    "Purchase User",
    "Item Manager",
    "HR User",  # read Employee: the worker PIN map
]

# The three floor warehouses. "Stores" is created by the setup wizard.
WAREHOUSES: list[dict[str, object]] = [
    {
        "warehouse_name": "Stores",
        "aliases": ["store", "stores", "ஸ்டோர்", "ஸ்டோர்ஸ்", "களஞ்சியம்", "ස්ටෝර්ස්", "ස්ටෝර්", "ගබඩාව"],
    },
    {
        "warehouse_name": "Line 1",
        "aliases": [
            "line one",
            "line 1",
            "லைன் ஒன்று",
            "லைன் 1",
            "முதல் லைன்",
            "ලයින් 1",
            "ලයින් වන්",
            "පළමු ලයින්",
        ],
    },
    {
        "warehouse_name": "Line 2",
        "aliases": [
            "line two",
            "line 2",
            "லைன் இரண்டு",
            "லைன் 2",
            "இரண்டாவது லைன்",
            "ලයින් 2",
            "ලයින් දෙක",
            "ලයින් ටූ",
            "දෙවන ලයින්",
        ],
    },
]

RM = "Raw Material"
FG = "Products"

# (item_code, item_name, item_group, uom, valuation_rate, aliases)
ITEMS: list[tuple[str, str, str, str, float, list[str]]] = [
    ("BRG-6201", "Ball Bearing 6201", RM, "Nos", 420, ["bearing 6201", "பேரிங்", "பேரிங் 6201"]),
    ("BRG-6204", "Ball Bearing 6204", RM, "Nos", 610, ["bearing 6204", "பேரிங்", "பேரிங் 6204"]),
    ("BRG-6205", "Ball Bearing 6205", RM, "Nos", 680, ["bearing 6205", "பேரிங்", "பேரிங் 6205"]),
    ("BRG-6305", "Ball Bearing 6305", RM, "Nos", 950, ["bearing 6305", "பேரிங்", "பேரிங் 6305"]),
    ("BLT-M8X30", "Hex Bolt M8x30", RM, "Nos", 25, ["bolt M8", "M8 bolt", "போல்ட்", "போல்ட் M8"]),
    ("BLT-M10X40", "Hex Bolt M10x40", RM, "Nos", 40, ["bolt M10", "M10 bolt", "போல்ட்", "போல்ட் M10"]),
    ("NUT-M8", "Hex Nut M8", RM, "Nos", 8, ["nut M8", "நட்", "நட் M8"]),
    ("NUT-M10", "Hex Nut M10", RM, "Nos", 12, ["nut M10", "நட்", "நட் M10"]),
    ("WSH-M8", "Flat Washer M8", RM, "Nos", 4, ["washer", "வாஷர்"]),
    ("SCR-M5X20", "Socket Cap Screw M5x20", RM, "Nos", 18, ["screw", "ஸ்க்ரூ", "திருகு"]),
    ("GR-SP40", "Spur Gear 40T", RM, "Nos", 2400, ["gear 40", "40 teeth gear", "கியர் 40"]),
    ("GR-SP60", "Spur Gear 60T", RM, "Nos", 3100, ["gear 60", "60 teeth gear", "கியர் 60"]),
    ("SPR-C20", "Compression Spring 20mm", RM, "Nos", 90, ["spring", "ஸ்பிரிங்"]),
    ("VBT-A42", "V-Belt A42", RM, "Nos", 850, ["v belt A42", "belt A42", "வி பெல்ட்"]),
    ("VBT-B50", "V-Belt B50", RM, "Nos", 1150, ["v belt B50", "belt B50", "வி பெல்ட் B50"]),
    ("SHF-25", "Shaft 25mm x 300", RM, "Nos", 1800, ["shaft", "ஷாஃப்ட்"]),
    ("CPL-L100", "Jaw Coupling L100", RM, "Nos", 2900, ["coupling", "கப்ளிங்"]),
    ("SEAL-35", "Oil Seal 35x52x7", RM, "Nos", 160, ["oil seal", "seal", "ஆயில் சீல்"]),
    ("ORG-20", "O-Ring 20mm NBR", RM, "Nos", 15, ["o ring", "ஓ ரிங்"]),
    ("GRS-EP2", "Grease EP2", RM, "Kg", 1400, ["grease", "கிரீஸ்"]),
    ("CHN-08B", "Roller Chain 08B", RM, "Meter", 1300, ["chain", "செயின்", "சங்கிலி"]),
    ("SPK-08B17", "Sprocket 08B 17T", RM, "Nos", 1650, ["sprocket", "ஸ்ப்ராக்கெட்"]),
    ("PLT-MS6", "MS Plate 6mm", RM, "Nos", 3500, ["MS plate", "plate", "பிளேட்", "தகடு"]),
    ("PIN-D6", "Dowel Pin 6mm", RM, "Nos", 10, ["dowel pin", "pin", "பின்"]),
    ("BSH-25", "Bronze Bush 25mm", RM, "Nos", 380, ["bush", "bronze bush", "புஷ்"]),
    ("KEY-8X7", "Parallel Key 8x7", RM, "Nos", 30, ["key", "parallel key", "கீ"]),
    ("CLP-H20", "Hose Clamp 20mm", RM, "Nos", 45, ["hose clamp", "clamp", "கிளாம்ப்"]),
    ("ASSY-PUMP", "Pump Assembly", FG, "Nos", 0, ["pump", "பம்ப்"]),
    ("ASSY-GBX", "Gearbox Assembly", FG, "Nos", 0, ["gearbox", "gear box", "கியர்பாக்ஸ்"]),
    ("ASSY-ROLL", "Conveyor Roller Assembly", FG, "Nos", 0, ["roller", "conveyor roller", "ரோலர்"]),
]

# Sinhala spoken names (transliterations as Voice Transcribe writes them, plus Sinhala words).
SI_ALIASES: dict[str, list[str]] = {
    "BRG-6201": ["බෙයාරිං", "බෙයාරිං 6201"], "BRG-6204": ["බෙයාරිං", "බෙයාරිං 6204"],
    "BRG-6205": ["බෙයාරිං", "බෙයාරිං 6205"], "BRG-6305": ["බෙයාරිං", "බෙයාරිං 6305"],
    "BLT-M8X30": ["බෝල්ට්", "බෝල්ට් M8"], "BLT-M10X40": ["බෝල්ට්", "බෝල්ට් M10"],
    "NUT-M8": ["නට්", "නට් M8"], "NUT-M10": ["නට්", "නට් M10"],
    "WSH-M8": ["වොෂර්"], "SCR-M5X20": ["ඉස්කුරුප්පු", "ස්ක්රූ"],
    "GR-SP40": ["ගියර් 40"], "GR-SP60": ["ගියර් 60"], "SPR-C20": ["ස්ප්රින්ග්"],
    "VBT-A42": ["වී බෙල්ට්", "බෙල්ට් A42"], "VBT-B50": ["වී බෙල්ට් B50", "බෙල්ට් B50"],
    "SHF-25": ["ෂැෆ්ට්"], "CPL-L100": ["කප්ලින්"], "SEAL-35": ["ඔයිල් සීල්", "සීල්"],
    "ORG-20": ["ඕ රින්"], "GRS-EP2": ["ග්රීස්"], "CHN-08B": ["චේන්", "දම්වැල"],
    "SPK-08B17": ["ස්ප්රොකට්"], "PLT-MS6": ["ප්ලේට්", "තහඩුව"], "PIN-D6": ["පින්"],
    "BSH-25": ["බුෂ්"], "KEY-8X7": ["කී"], "CLP-H20": ["ක්ලෑම්ප්"],
    "ASSY-PUMP": ["පොම්පය", "පම්ප්"], "ASSY-GBX": ["ගියර් බොක්ස්"], "ASSY-ROLL": ["රෝලර්"],
}  # fmt: skip
ITEMS = [(c, n, g, u, r, [*a, *SI_ALIASES.get(c, [])]) for c, n, g, u, r, a in ITEMS]

# Opening stock: warehouse -> {item_code: qty}
OPENING_STOCK: dict[str, dict[str, float]] = {
    "Stores": {
        "BRG-6201": 120, "BRG-6204": 80, "BRG-6205": 60, "BRG-6305": 30,
        "BLT-M8X30": 1000, "BLT-M10X40": 600, "NUT-M8": 1500, "NUT-M10": 800,
        "WSH-M8": 2000, "SCR-M5X20": 500, "GR-SP40": 25, "GR-SP60": 20,
        "SPR-C20": 200, "VBT-A42": 40, "VBT-B50": 25, "SHF-25": 50,
        "CPL-L100": 15, "SEAL-35": 150, "ORG-20": 400, "GRS-EP2": 40,
        "CHN-08B": 100, "SPK-08B17": 30, "PLT-MS6": 20, "PIN-D6": 600,
        "BSH-25": 70, "KEY-8X7": 300, "CLP-H20": 250,
    },
    "Line 1": {"BRG-6205": 6, "BLT-M8X30": 100, "SEAL-35": 10},
    "Line 2": {"BRG-6201": 10, "SHF-25": 4, "GRS-EP2": 3},
}  # fmt: skip

# What SAM 3.1 looks for when an item is counted by video (Item.floorvoice_count_prompt).
# One short noun phrase per item, describing what the camera sees, not what is inside: SAM
# takes one concept per request, returns nothing for lists ("bolt, nut") and reads words
# literally. On a 12-box pan "box" and "brown cardboard box" found 12/12 every run;
# "bearing box", "small box" found nothing, "cardboard box" 9-10.
COUNT_PROMPTS: dict[str, str] = {
    "BRG-6201": "box", "BRG-6204": "box", "BRG-6205": "box",
    "BRG-6305": "box", "BLT-M8X30": "bolt packet", "BLT-M10X40": "bolt packet",
    "NUT-M8": "nut packet", "NUT-M10": "nut packet", "WSH-M8": "washer packet",
    "SCR-M5X20": "box", "GR-SP40": "spur gear", "GR-SP60": "spur gear",
    "SPR-C20": "coil spring", "VBT-A42": "rubber v-belt", "VBT-B50": "rubber v-belt",
    "SHF-25": "steel shaft", "CPL-L100": "box", "SEAL-35": "oil seal",
    "ORG-20": "o-ring packet", "GRS-EP2": "grease tub", "CHN-08B": "roller chain",
    "SPK-08B17": "sprocket", "PLT-MS6": "steel plate", "PIN-D6": "pin packet",
    "BSH-25": "bronze bush", "KEY-8X7": "key packet", "CLP-H20": "hose clamp",
    "ASSY-PUMP": "pump", "ASSY-GBX": "gearbox", "ASSY-ROLL": "conveyor roller",
}  # fmt: skip

# Video-count demo: ERPNext's Stores quantity for these two items is deliberately a few
# units above what is on the demo shelf (12 boxes of 6305, 8 coupling boxes), so the count
# has something to correct. Seeded as a Material Issue that demo_reset keeps.
COUNT_DEMO: dict[str, float] = {"BRG-6305": 15, "CPL-L100": 10}

SUPPLIERS = [
    {"supplier_name": "Lanka Bearings & Tools", "aliases": ["Lanka Bearings"]},
    {"supplier_name": "Jaffna Industrial Supplies", "aliases": ["Jaffna Industrial"]},
]

OPERATIONS = ["Assembly", "Machining"]

WORKSTATIONS = [
    {"workstation_name": "Assembly Bench L1", "warehouse": "Line 1", "hour_rate": 1500},
    {"workstation_name": "CNC Lathe L2", "warehouse": "Line 2", "hour_rate": 2500},
]

# item -> (operation, workstation, minutes per unit, [(component, qty per unit)])
BOMS: dict[str, tuple[str, str, float, list[tuple[str, float]]]] = {
    "ASSY-PUMP": (
        "Assembly",
        "Assembly Bench L1",
        12,
        [("BRG-6205", 2), ("SEAL-35", 1), ("SHF-25", 1), ("BLT-M8X30", 4)],
    ),
    "ASSY-GBX": (
        "Machining",
        "CNC Lathe L2",
        30,
        [("GR-SP40", 1), ("GR-SP60", 1), ("BRG-6204", 2), ("SHF-25", 1), ("KEY-8X7", 2)],
    ),
    "ASSY-ROLL": (
        "Assembly",
        "CNC Lathe L2",
        8,
        [("BRG-6201", 2), ("SHF-25", 1), ("SEAL-35", 2)],
    ),
}

# Five open work orders -> five open job cards. (item, qty, wip warehouse)
WORK_ORDERS = [
    ("ASSY-PUMP", 50, "Line 1"),
    ("ASSY-GBX", 20, "Line 2"),
    ("ASSY-ROLL", 100, "Line 2"),
    ("ASSY-PUMP", 30, "Line 1"),
    ("ASSY-GBX", 10, "Line 2"),
]

# Shop-floor workers (ERPNext Employee). The PIN is only used here: the seed stores its salted
# hash in Employee.floorvoice_pin. home = home line (default destination / requesting
# warehouse); language = preferred speech + readback language.
WORKERS: list[dict[str, str]] = [
    {"first_name": "Kumar", "last_name": "Selvaraj", "gender": "Male", "pin": "1111",
     "home": "Line 1", "language": "ta", "aliases": "kumar, குமார்"},
    {"first_name": "Priya", "last_name": "Nadarajah", "gender": "Female", "pin": "2222",
     "home": "Line 2", "language": "ta", "aliases": "priya, பிரியா"},
    {"first_name": "Nimal", "last_name": "Perera", "gender": "Male", "pin": "3333",
     "home": "Line 1", "language": "si", "aliases": "nimal, නිමල්"},
    {"first_name": "Dilani", "last_name": "Fernando", "gender": "Female", "pin": "4444",
     "home": "Line 2", "language": "si", "aliases": "dilani, දිලානි"},
    {"first_name": "Arjun", "last_name": "Mahendran", "gender": "Male", "pin": "5555",
     "home": "Stores", "language": "en", "aliases": "arjun"},
    {"first_name": "Sarah", "last_name": "Thomas", "gender": "Female", "pin": "6666",
     "home": "Line 1", "language": "en", "aliases": "sarah"},
]  # fmt: skip
