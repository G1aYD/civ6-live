from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from .archive import (
    DEAL_GOLD_RE,
    DEAL_TURN_RE,
    DEFAULT_WONDER_TYPES,
    PANTHEON_RE,
    latest_deal_session_lines,
)
from .bbg_zh_names import BBG_ZH_NAME_MAP
from .config import Civ6Paths
from .events import format_events, human_live_events
from .model import GameSnapshot, PlayerTurnStats, clean_game_name
from .parsers import latest_text_session_lines
from .query import describe_player
from .save_reader import SaveParseError, read_latest_save_summary
from .watch_intel import format_game_token, latest_session_csv_dicts, parse_int, read_csv_dicts


CITY_STATE_RE = re.compile(
    r"Player\s+(?P<slot>\d+):\s+Civilization\s+-\s+(?P<civ>CIVILIZATION_[A-Z0-9_]+).*?"
    r"Leader\s+-\s+(?P<leader>LEADER_MINOR_CIV_[A-Z0-9_]+).*?"
    r"Level\s+-\s+CIVILIZATION_LEVEL_CITY_STATE"
)
G_CHRONICLES_PATH = Path(__file__).with_name("g_chronicles.txt")
G_CHRONICLES_TRIGGERS = (
    "glayd",
    "glaid",
    "g史记",
    "g神",
    "g鳖",
    "g宝",
    "gla",
    "主播",
    "以弗所",
    "必得",
)
CURRENT_GAME_KEYWORDS = (
    "当前",
    "现在",
    "本局",
    "这局",
    "比赛",
    "文明",
    "领袖",
    "玩家",
    "队伍",
    "几队",
    "回合",
    "金币",
    "回合金",
    "现金",
    "科技",
    "文化",
    "产能",
    "粮",
    "信仰",
    "旅游",
    "胜利",
    "宗教",
    "外交",
    "城邦",
    "奇观",
    "伟人",
    "大科",
    "大工",
    "大商",
    "大军",
    "总督",
    "城市",
    "首都",
    "学院",
    "剧院",
    "军营",
    "骑士",
    "单位",
    "兵",
    "周围",
    "旁边",
    "位置",
    "地块",
    "奢侈",
    "战略",
    "朝鲜",
    "韩国",
    "克里",
    "中国",
    "波兰",
    "法国",
    "刚果",
    "俄罗斯",
    "阿拉伯",
)

OVERLAY_GOLD_NEWS_RE = re.compile(
    r"(?P<turn>\d+)T\s+送出\s+(?P<amount>[0-9]+(?:\.[0-9]+)?)\s+"
    r"(?P<kind>金币|回合金)(?:（持续\s*(?P<duration>\d+)T）)?"
)
TEXT_GOLD_NEWS_RE = re.compile(
    r"(?P<turn>\d+)T\s+(?P<from>.+?)\s+向\s+(?P<to>.+?)\s+送出\s+"
    r"(?P<amount>[0-9]+(?:\.[0-9]+)?)\s+"
    r"(?P<kind>金币|回合金)(?:（持续\s*(?P<duration>\d+)T）)?"
)


CITY_STATE_TYPES = {
    "Akkad": ("Militaristic", "军事"),
    "Antananarivo": ("Cultural", "文化"),
    "Armagh": ("Religious", "宗教"),
    "Auckland": ("Industrial", "工业"),
    "Babylon": ("Scientific", "科技"),
    "Bologna": ("Scientific", "科技"),
    "Buenos Aires": ("Industrial", "工业"),
    "Cahokia": ("Trade", "商业"),
    "Cardiff": ("Industrial", "工业"),
    "Fez": ("Scientific", "科技"),
    "Geneva": ("Scientific", "科技"),
    "Granada": ("Militaristic", "军事"),
    "Hunza": ("Trade", "商业"),
    "Kabul": ("Militaristic", "军事"),
    "Kandy": ("Religious", "宗教"),
    "Kumasi": ("Cultural", "文化"),
    "La Venta": ("Religious", "宗教"),
    "Lahore": ("Militaristic", "军事"),
    "Lisbon": ("Trade", "商业"),
    "Mexico City": ("Industrial", "工业"),
    "Mitla": ("Scientific", "科技"),
    "Mogadishu": ("Trade", "商业"),
    "Muscat": ("Trade", "商业"),
    "Nalanda": ("Scientific", "科技"),
    "Nan Madol": ("Cultural", "文化"),
    "Nazca": ("Religious", "宗教"),
    "Ngazargamu": ("Militaristic", "军事"),
    "Preslav": ("Militaristic", "军事"),
    "Rapa Nui": ("Cultural", "文化"),
    "Singapore": ("Industrial", "工业"),
    "Stockholm": ("Scientific", "科技"),
    "Taruga": ("Scientific", "科技"),
    "Valletta": ("Militaristic", "军事"),
    "Vatican City": ("Religious", "宗教"),
    "Vilnius": ("Cultural", "文化"),
    "Wolin": ("Militaristic", "军事"),
    "Yerevan": ("Religious", "宗教"),
    "Zanzibar": ("Trade", "商业"),
}

CITY_STATE_TYPES.update(
    {
        "Antioch": ("Trade", "\u5546\u4e1a"),
        "Jerusalem": ("Religious", "\u5b97\u6559"),
        "Johannesburg": ("Industrial", "\u5de5\u4e1a"),
        "Bandar Brunei": ("Trade", "\u5546\u4e1a"),
        "Ayutthaya": ("Cultural", "\u6587\u5316"),
        "Caguana": ("Cultural", "\u6587\u5316"),
        "Chinguetti": ("Religious", "\u5b97\u6559"),
        "Hattusa": ("Scientific", "\u79d1\u6280"),
        "Mitla": ("Scientific", "\u79d1\u6280"),
        "Moheno Daro": ("Cultural", "\u6587\u5316"),
        "Mohenjo Daro": ("Cultural", "\u6587\u5316"),
        "Palenque": ("Scientific", "\u79d1\u6280"),
        "Samarkand": ("Trade", "\u5546\u4e1a"),
    }
)


DISTRICT_GROUPS = {
    "DISTRICT_CAMPUS": "Campus",
    "DISTRICT_SEOWON": "Campus",
    "DISTRICT_OBSERVATORY": "Campus",
    "DISTRICT_HOLY_SITE": "Holy Site",
    "DISTRICT_COMMERCIAL_HUB": "Commercial Hub",
    "DISTRICT_HARBOR": "Harbor",
    "DISTRICT_THEATER": "Theater Square",
    "DISTRICT_THEATRE": "Theater Square",
    "DISTRICT_INDUSTRIAL_ZONE": "Industrial Zone",
    "DISTRICT_ENCAMPMENT": "Encampment",
    "DISTRICT_ENTERTAINMENT_COMPLEX": "Entertainment Complex",
    "DISTRICT_WATER_ENTERTAINMENT_COMPLEX": "Water Park",
    "DISTRICT_AERODROME": "Aerodrome",
    "DISTRICT_NEIGHBORHOOD": "Neighborhood",
    "DISTRICT_AQUEDUCT": "Aqueduct",
    "DISTRICT_BATH": "Aqueduct",
    "DISTRICT_DAM": "Dam",
    "DISTRICT_CANAL": "Canal",
    "DISTRICT_GOVERNMENT": "Government Plaza",
    "DISTRICT_DIPLOMATIC_QUARTER": "Diplomatic Quarter",
}


DISTRICT_ZH = {
    "Campus": "\u5b66\u9662\u533a",
    "Seowon": "\u4e66\u9662",
    "Observatory": "\u5929\u6587\u53f0",
    "Holy Site": "\u5723\u5730",
    "Commercial Hub": "\u5546\u4e1a\u4e2d\u5fc3",
    "Harbor": "\u6e2f\u53e3",
    "Theater Square": "\u5267\u9662\u5e7f\u573a",
    "Theater": "\u5267\u9662\u5e7f\u573a",
    "Industrial Zone": "\u5de5\u4e1a\u533a",
    "Encampment": "\u519b\u8425",
    "Entertainment Complex": "\u5a31\u4e50\u4e2d\u5fc3",
    "Water Park": "\u6c34\u4e0a\u4e50\u56ed",
    "Aerodrome": "\u673a\u573a",
    "Neighborhood": "\u793e\u533a",
    "Government Plaza": "\u653f\u5e9c\u5e7f\u573a",
    "Government": "\u653f\u5e9c\u5e7f\u573a",
    "Diplomatic Quarter": "\u5916\u4ea4\u533a",
    "Aqueduct": "\u6c34\u6e20",
    "Dam": "\u5927\u575d",
    "Canal": "\u8fd0\u6cb3",
    "Water Entertainment Complex": "\u6c34\u4e0a\u4e50\u56ed",
    "City Center": "\u5e02\u4e2d\u5fc3",
    "Wonder": "\u5947\u89c2",
}


PRODUCTION_TYPE_ZH = {
    "Unit": "\u5355\u4f4d",
    "Building": "\u5efa\u7b51",
    "District": "\u533a\u57df",
    "Wonder": "\u5947\u89c2",
    "Project": "\u9879\u76ee",
}


CITY_NAME_ZH_OVERRIDES = {
    "LOC_CITY_NAME_JINJU": "\u664b\u5dde",
    "LOC_CITY_NAME_GYEONGJU": "\u5e86\u5dde",
    "LOC_CITY_NAME_GANGNEUNG": "\u6c5f\u9675",
    "LOC_CITY_NAME_GWANGJU": "\u5149\u5dde",
    "LOC_CITY_NAME_YANGSAN": "\u6881\u5c71",
    "LOC_CITY_NAME_SUWON": "\u6c34\u539f",
    "LOC_CITY_NAME_MIKISIW_WACIHK": "\u7c73\u57fa\u897f\u74e6\u5947\u8d6b\u514b",
    "LOC_CITY_NAME_MISTAHI_SIPIHK": "\u7c73\u65af\u5854\u5e0c\u897f\u76ae\u8d6b\u514b",
    "LOC_CITY_NAME_PEEPEEKISIS": "\u76ae\u76ae\u57fa\u897f\u65af",
    "LOC_CITY_NAME_PASKWAW_ASKIHK": "\u5e15\u65af\u514b\u74e6\u963f\u65af\u57fa\u8d6b\u514b",
    "LOC_CITY_NAME_PIHTOKAHANAPIWIYIN": "\u76ae\u8d6b\u6258\u5361\u54c8\u7eb3\u76ae\u7ef4\u56e0",
    "LOC_CITY_NAME_AHTAHKAKOOP": "\u963f\u5854\u5361\u5e93\u666e",
    "LOC_CITY_NAME_PIYESIW_AWASIS": "\u76ae\u8036\u897f\u4e4c\u963f\u74e6\u897f\u65af",
    "LOC_CITY_NAME_MAKWA_SAKAHIKAN": "\u9a6c\u5938\u8428\u5361\u5e0c\u574e",
    "LOC_CITY_NAME_KRAKOW": "\u514b\u62c9\u79d1\u592b",
    "LOC_CITY_NAME_LUBLIN": "\u5362\u5e03\u6797",
    "LOC_CITY_NAME_BYDGOSZCZ": "\u6bd4\u5f97\u54e5\u4ec0",
    "LOC_CITY_NAME_RADOM": "\u62c9\u591a\u59c6",
    "LOC_CITY_NAME_LODZ": "\u7f57\u5179",
    "LOC_CITY_NAME_POZNAN": "\u6ce2\u5179\u5357",
    "LOC_CITY_NAME_AIGAI": "\u57c3\u76d6",
    "LOC_CITY_NAME_METHONE": "\u58a8\u6258\u6d85",
    "LOC_CITY_NAME_ALEXANDRIA_TROAS": "\u7279\u6d1b\u963f\u65af\u7684\u4e9a\u5386\u5c71\u5927",
    "LOC_CITY_NAME_ALEXANDRIA": "\u4e9a\u5386\u5c71\u5927",
    "LOC_CITY_NAME_ALEXANDRETTA": "\u4e9a\u5386\u5c71\u5fb7\u52d2\u5854",
    "LOC_CITY_NAME_CHALKIDIKI": "\u54c8\u5c14\u57fa\u5b63\u57fa",
    "LOC_CITY_NAME_ALEXANDRIA_ARACHOSIA": "\u963f\u62c9\u970d\u897f\u4e9a\u7684\u4e9a\u5386\u5c71\u5927",
    "Jinju": "\u664b\u5dde",
    "Gyeongju": "\u5e86\u5dde",
    "Gangneung": "\u6c5f\u9675",
    "Gwangju": "\u5149\u5dde",
    "Yangsan": "\u6881\u5c71",
    "Suwon": "\u6c34\u539f",
    "Krakow": "\u514b\u62c9\u79d1\u592b",
    "Lublin": "\u5362\u5e03\u6797",
    "Bydgoszcz": "\u6bd4\u5f97\u54e5\u4ec0",
    "Radom": "\u62c9\u591a\u59c6",
    "Lodz": "\u7f57\u5179",
    "Poznan": "\u6ce2\u5179\u5357",
    "Aigai": "\u57c3\u76d6",
    "Methone": "\u58a8\u6258\u6d85",
    "Alexandria Troas": "\u7279\u6d1b\u963f\u65af\u7684\u4e9a\u5386\u5c71\u5927",
    "Alexandria": "\u4e9a\u5386\u5c71\u5927",
    "Alexandretta": "\u4e9a\u5386\u5c71\u5fb7\u52d2\u5854",
    "Chalkidiki": "\u54c8\u5c14\u57fa\u5b63\u57fa",
    "Alexandria Arachosia": "\u963f\u62c9\u970d\u897f\u4e9a\u7684\u4e9a\u5386\u5c71\u5927",
}


GOVERNOR_ZH = {
    "The Cardinal": "\u83ab\u514b\u590f",
    "The Educator": "\u5e73\u4f3d\u62c9",
    "The Resource Manager": "\u9a6c\u683c\u52aa\u65af",
    "The Builder": "\u6881",
    "The Merchant": "\u745e\u5a1c",
    "The Defender": "\u7ef4\u514b\u591a",
    "The Ambassador": "\u963f\u739b\u5c3c",
    "Ibrahim": "\u6613\u535c\u62c9\u6b23",
}


ZH_NAME_MAP = {
    "Arabia": "阿拉伯",
    "Russia": "俄罗斯",
    "Vietnam": "越南",
    "Brazil": "巴西",
    "America": "美国",
    "Australia": "澳大利亚",
    "Aztec": "阿兹特克",
    "Babylon": "巴比伦",
    "Byzantium": "拜占庭",
    "Canada": "加拿大",
    "China": "中国",
    "Cree": "克里",
    "Egypt": "埃及",
    "England": "英格兰",
    "Ethiopia": "埃塞俄比亚",
    "France": "法国",
    "Gaul": "高卢",
    "Georgia": "格鲁吉亚",
    "Germany": "德国",
    "Gran Colombia": "大哥伦比亚",
    "Greece": "希腊",
    "Hungary": "匈牙利",
    "Inca": "印加",
    "India": "印度",
    "Indonesia": "印度尼西亚",
    "Japan": "日本",
    "Khmer": "高棉",
    "Kongo": "刚果",
    "Korea": "朝鲜",
    "Macedon": "马其顿",
    "Mali": "马里",
    "Maori": "毛利",
    "Mapuche": "马普切",
    "Maya": "玛雅",
    "Mongolia": "蒙古",
    "Netherlands": "荷兰",
    "Norway": "挪威",
    "Nubia": "努比亚",
    "Ottoman": "奥斯曼",
    "Persia": "波斯",
    "Phoenicia": "腓尼基",
    "Poland": "波兰",
    "Portugal": "葡萄牙",
    "Rome": "罗马",
    "Scotland": "苏格兰",
    "Scythia": "斯基泰",
    "Spain": "西班牙",
    "Sumeria": "苏美尔",
    "Sweden": "瑞典",
    "Zulu": "祖鲁",
    "Lime Teotihuacan": "特奥蒂瓦坎",
    "Teotihuacan": "特奥蒂瓦坎",
    "Saladin": "萨拉丁",
    "Peter Great": "彼得大帝",
    "Lady Trieu": "赵夫人",
    "Pedro": "佩德罗",
    "Pericles": "伯里克利",
    "Gorgo": "戈尔戈",
    "Spearthrower Owl": "投矛者枭",
    "Lime Teo Owl": "投矛者枭",
    "Abraham Lincoln": "亚伯拉罕·林肯",
    "Teddy Roosevelt": "泰迪·罗斯福",
    "John Curtin": "约翰·科廷",
    "Montezuma": "蒙特祖玛",
    "Hammurabi": "汉谟拉比",
    "Basil": "巴西尔二世",
    "Theodora": "狄奥多拉",
    "Wilfrid Laurier": "威尔弗里德·劳雷尔",
    "Kublai Khan": "忽必烈",
    "Qin": "秦始皇",
    "Wu Zetian": "武则天",
    "Yongle": "永乐",
    "Poundmaker": "庞德梅克",
    "Cleopatra": "克利奥帕特拉",
    "Ramses": "拉美西斯二世",
    "Eleanor": "阿基坦的埃莉诺",
    "Elizabeth": "伊丽莎白一世",
    "Victoria": "维多利亚",
    "Menelik": "孟尼利克二世",
    "Catherine De Medici": "凯瑟琳·德·美第奇",
    "Ambiorix": "安比奥里克斯",
    "Tamar": "塔玛丽",
    "Barbarossa": "腓特烈·巴巴罗萨",
    "Ludwig": "路德维希二世",
    "Simon Bolivar": "西蒙·玻利瓦尔",
    "Matthias Corvinus": "马加什·科尔温",
    "Pachacuti": "帕查库特克",
    "Chandragupta": "旃陀罗笈多",
    "Gandhi": "甘地",
    "Gitarja": "吉塔迦",
    "Hojo": "北条时宗",
    "Tokugawa": "德川家康",
    "Jayavarman": "阇耶跋摩七世",
    "Mvemba": "姆本巴·恩津加",
    "Nzinga Mbande": "恩津加·姆班德",
    "Sejong": "世宗",
    "Seondeok": "善德女王",
    "Alexander": "亚历山大",
    "Jfd Olympias": "奥林匹亚丝",
    "Mansa Musa": "曼萨·穆萨",
    "Sundiata Keita": "松迪亚塔·凯塔",
    "Kupe": "库佩",
    "Lautaro": "莱夫扎茹",
    "Lady Six Sky": "六日夫人",
    "Genghis Khan": "成吉思汗",
    "Wilhelmina": "威廉明娜",
    "Harald Hardrada": "哈拉尔德·哈德拉达",
    "Amanitore": "阿曼尼托尔",
    "Suleiman": "苏莱曼",
    "Cyrus": "居鲁士",
    "Nader Shah": "纳迪尔沙阿",
    "Dido": "狄多",
    "Jadwiga": "雅德维加",
    "Joao Iii": "若昂三世",
    "Trajan": "图拉真",
    "Julius Caesar": "尤利乌斯·凯撒",
    "Robert The Bruce": "罗伯特一世",
    "Tomyris": "托米丽司",
    "Philip Ii": "腓力二世",
    "Gilgamesh": "吉尔伽美什",
    "Kristina": "克里斯蒂娜",
    "Shaka": "恰卡",
    "Hunza": "罕萨",
    "Vatican City": "梵蒂冈城",
    "Auckland": "奥克兰",
    "La Venta": "拉文塔",
    "Nazca": "纳斯卡",
    "Kandy": "康提",
    "Nalanda": "那烂陀",
    "Buenos Aires": "布宜诺斯艾利斯",
    "Ngazargamu": "恩加扎尔加穆",
    "Bologna": "博洛尼亚",
    "Vilnius": "维尔纽斯",
    "Yerevan": "埃里温",
    "Taruga": "塔鲁加",
    "Muscat": "马斯喀特",
    "Cairo": "开罗",
    "Jeddah": "吉达",
    "St Petersburg": "圣彼得堡",
    "Hanging Gardens": "空中花园",
    "Temple Artemis": "阿尔忒弥斯神庙",
    "Great Bath": "大浴场",
    "Stonehenge": "巨石阵",
    "Pyramids": "金字塔",
    "Oracle": "神谕",
    "Etemenanki": "埃特曼安吉神庙",
    "Machu Picchu": "马丘比丘",
    "Mausoleum At Halicarnassus": "摩索拉斯王陵",
    "Colosseum": "罗马斗兽场",
    "Kilwa Kisiwani": "基尔瓦基斯瓦尼",
    "Mahabodhi Temple": "摩诃菩提寺",
    "Petra": "佩特拉古城",
    "Great Lighthouse": "大灯塔",
    "Great Library": "大图书馆",
    "Apadana": "阿帕达纳宫",
    "Terracotta Army": "兵马俑",
    "Alhambra": "阿尔罕布拉宫",
    "Angkor Wat": "吴哥窟",
    "Big Ben": "大本钟",
    "Bolshoi Theatre": "莫斯科大剧院",
    "Broadway": "百老汇",
    "Casa De Contratacion": "西印度交易所",
    "Chichen Itza": "奇琴伊察",
    "Cristo Redentor": "救世基督像",
    "Eiffel Tower": "埃菲尔铁塔",
    "Estadio Do Maracana": "马拉卡纳体育场",
    "Forbidden City": "紫禁城",
    "Hagia Sophia": "圣索菲亚大教堂",
    "Hermitage": "冬宫",
    "Huey Teocalli": "大神庙",
    "Jebel Barkal": "博尔戈尔山",
    "Meenakshi Temple": "米纳克希神庙",
    "Mont St Michel": "圣米歇尔山",
    "Oxford University": "牛津大学",
    "Potala Palace": "布达拉宫",
    "Ruhr Valley": "鲁尔区",
    "Taj Mahal": "泰姬陵",
    "Torre De Belem": "贝伦塔",
    "University Of Sankore": "桑科雷大学",
    "Venetian Arsenal": "威尼斯军械库",
    "Goddess Of The Hunt": "狩猎女神",
    "Goddess Of Festivals": "节庆女神",
    "Divine Spark": "神圣火花",
    "Religious Settlements": "宗教移民",
    "Earth Goddess": "大地女神",
    "God Of The Sea": "海洋之神",
    "God Of The Forge": "锻造之神",
    "Lady Of The Reeds And Marshes": "芦苇与沼泽女神",
    "River Goddess": "河神",
    "Fertility Rites": "丰产仪式",
    "God Of War": "战争之神",
    "God Of Craftsmen": "工匠之神",
    "God Of Healing": "治愈之神",
    "God Of The Open Sky": "苍天之神",
    "God Of The City": "城市守护神",
    "Monument To The Gods": "众神纪念碑",
    "Religious Idols": "宗教偶像",
    "Sacred Path": "神圣道路",
    "Dance Of The Aurora": "极光之舞",
    "Desert Folklore": "沙漠民俗",
    "Initiation Rites": "入会仪式",
    "Confucius": "孔子",
    "Simon Peter": "西门彼得",
    "Prophet": "大预言家",
    "Classical": "古典时代",
    "Ancient": "远古时代",
    "Medieval": "中古时代",
    "Renaissance": "文艺复兴时代",
    "Industrial": "工业时代",
    "Modern": "现代",
    "Atomic": "原子能时代",
    "Information": "信息时代",
    "Catholicism": "天主教",
    "Confucianism": "儒教",
}

ZH_NAME_MAP.update(
    {
        "Antioch": "\u5b89\u6761\u514b",
        "Jerusalem": "\u8036\u8def\u6492\u51b7",
        "Singapore": "\u65b0\u52a0\u5761",
        "Akkad": "\u963f\u5361\u5fb7",
        "Johannesburg": "\u7ea6\u7ff0\u5185\u65af\u5821",
    }
)
ZH_NAME_MAP.update(BBG_ZH_NAME_MAP)


def llm_context_json(paths: Civ6Paths, snapshot: GameSnapshot, turn: int | None = None, limit: int = 30) -> str:
    return json.dumps(build_llm_context(paths, snapshot, turn=turn, limit=limit), ensure_ascii=False, indent=2)


def llm_context_prompt(
    paths: Civ6Paths,
    snapshot: GameSnapshot,
    turn: int | None = None,
    limit: int = 30,
    question: str | None = None,
) -> str:
    context = build_llm_context(paths, snapshot, turn=turn, limit=limit)
    sections = [
        "以下是文明 6 当前局势 JSON。回答时优先使用 *_zh、display_zh、turn_label 字段。",
        "JSON 仅供内部理解；面向观众回答时不要提 JSON 字段名、文件名、日志名或“某字段为空”。",
        "如果问题问谁花钱/送钱/打钱最多，优先看金币摘要和转账总计；若没有转账记录，用经济数据估计，不要回答内部字段为空。",
        "金币交易必须区分现金和回合金：duration=0/timing=one_time 是一次性现金；duration>0/timing=per_turn 是每回合金币，不要混算。",
        "金币统计里 totals_sent_desc 只代表一次性现金；gpt_sent_desc 只代表回合金。",
        "",
        "```json",
        json.dumps(context, ensure_ascii=False, separators=(",", ":")),
        "```",
    ]
    if should_include_g_chronicles(question):
        sections.extend(
            [
                "",
                "以下是“G史记”梗资料。仅当观众询问 GLaYD、主播个人梗，或问题与当前比赛局势无直接关系时参考。",
                "使用时可以幽默，但不要逐字长篇复述；不要把这些历史梗当成当前比赛事实。",
                load_g_chronicles(),
            ]
        )
    return "\n".join(sections)


def build_llm_context(paths: Civ6Paths, snapshot: GameSnapshot, turn: int | None = None, limit: int = 30) -> dict:
    selected_turn = snapshot.latest_turn if turn is None else turn
    rows = [
        stats for stats in snapshot.stats_for_turn(selected_turn)
        if stats.slot is not None and snapshot.identity_for(stats.slot) is not None
    ]
    rows.sort(key=lambda stats: stats.slot if stats.slot is not None else 9999)

    return {
        "schema": "civ6intel.llm_context.v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "bbg_reference_url": "https://civ6bbg.github.io/",
        "observer_mode": {
            "enabled": True,
            "note": "Full visible game-state context may be used for live commentary and Q&A.",
        },
        "latest_turn": snapshot.latest_turn,
        "latest_turn_label": turn_label(snapshot.latest_turn),
        "selected_turn": selected_turn,
        "selected_turn_label": turn_label(selected_turn),
        "save_file": save_file_context(paths, snapshot, limit=limit),
        "context_notes_zh": [
            "*_zh、display_zh、turn_label 是首选展示字段。",
            "胜利进度是近似指标；文化游客阈值、宗教皈依总数、统治首都归属暂未精确解析。",
            "分数和产出多来自回合刷新；回合内事件可能不完整。",
        ],
        "position_answering_rule_zh": "\u65b9\u4f4d/\u5468\u56f4\u662f\u8c01\u7684\u95ee\u9898\u4f18\u5148\u4f7f\u7528 save_file.player_details.players[].location\uff1blocation.center \u9ed8\u8ba4\u4f7f\u7528\u539f\u59cb\u9996\u90fd/\u51fa\u751f\u5730\u7f18\uff0c\u5373\u4fbf\u8be5\u9996\u90fd\u5df2\u88ab\u522b\u4eba\u5360\u9886\uff1b\u95ee\u5f53\u524d\u6b8b\u4f59\u57ce\u5e02\u624d\u770b current_core_center\uff1b\u672c\u5b58\u6863\u5750\u6807 y \u8d8a\u5927\u8d8a\u5317\uff0cy \u8d8a\u5c0f\u8d8a\u5357\uff0c\u8ddd\u79bb\u4f7f\u7528 hexes\u3002",
        "players": [player_context(snapshot, stats) for stats in rows],
        "teams": team_context(snapshot, rows),
        "leaders": leader_context(snapshot, rows),
        "victory_progress": victory_context(snapshot, rows),
        "city_states": city_states_context(paths),
        "pantheons": pantheon_context(paths, snapshot),
        "great_people": great_people_context(paths, snapshot, limit=limit),
        "wonders": wonder_context(paths, snapshot, limit=limit),
        "districts_and_builds": districts_and_builds_context(paths, snapshot, rows, limit=limit),
        "governors": governors_context(paths, snapshot, limit=limit),
        "gold": gold_context(paths, snapshot),
        "recent_important_events": recent_events_context(paths, limit=limit),
        "warnings": snapshot.warnings,
    }


def should_include_g_chronicles(question: str | None) -> bool:
    if not question:
        return False
    normalized = question.casefold()
    if any(trigger in normalized for trigger in G_CHRONICLES_TRIGGERS):
        return True
    return not any(keyword in question for keyword in CURRENT_GAME_KEYWORDS)


@lru_cache(maxsize=1)
def load_g_chronicles() -> str:
    try:
        text = G_CHRONICLES_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""
    return text.strip()


def player_context(snapshot: GameSnapshot, stats: PlayerTurnStats) -> dict:
    identity = snapshot.identity_for(stats.slot)
    score_categories = {key: value for key, value in sorted(stats.score_categories.items())}
    return {
        "slot": stats.slot,
        "player_name": identity.player_name if identity else None,
        "network_name": identity.network_name if identity else None,
        "team": identity.team if identity else None,
        "team_label": team_label(identity.team) if identity else None,
        "display": describe_player(snapshot, stats),
        "display_zh": player_label_zh(snapshot, stats.slot),
        "civilization": identity.short_civ if identity else stats.raw_player,
        "civilization_zh": zh_name(identity.short_civ if identity else stats.raw_player),
        "leader": identity.short_leader if identity else None,
        "leader_zh": zh_name(identity.short_leader) if identity else None,
        "turn": stats.turn,
        "turn_label": turn_label(stats.turn),
        "cities": stats.cities,
        "population": stats.population,
        "techs": stats.techs,
        "civics": stats.civics,
        "gold_balance": stats.gold_balance,
        "faith_balance": stats.faith_balance,
        "science_per_turn": stats.science_yield,
        "culture_per_turn": stats.culture_yield,
        "gold_per_turn": stats.gold_yield,
        "faith_per_turn": stats.faith_yield,
        "production_per_turn": stats.production_yield,
        "food_per_turn": stats.food_yield,
        "tourism": stats.tourism,
        "diplo_victory_points": stats.diplo_victory_points,
        "score": stats.score,
        "score_categories": score_categories,
    }


def save_file_context(paths: Civ6Paths, snapshot: GameSnapshot, limit: int) -> dict:
    summary = read_latest_save_summary(paths, include_map=True)
    if not summary:
        return {"available": False}
    save_identities = save_identity_context(summary)
    major_slots = {
        player.get("slot")
        for player in summary.get("major_players", [])
        if player.get("slot") is not None
    }

    return {
        "available": True,
        "source": summary.get("source"),
        "file": summary.get("file"),
        "turn": summary.get("turn"),
        "turn_label": turn_label(summary.get("turn")),
        "parse_ms": summary.get("parse_ms"),
        "game_seed": summary.get("game_seed"),
        "major_players": [
            save_player_context(player)
            for player in summary.get("major_players", [])
        ],
        "teams": save_teams_llm_context(summary.get("teams", {})),
        "active_city_states": [
            save_city_state_context(city_state)
            for city_state in summary.get("active_city_states", [])
        ][:limit],
        "map": save_map_context(summary.get("map"), save_identities, limit=limit),
        "player_details": save_player_details_context(
            paths,
            summary.get("player_details"),
            save_identities,
            major_slots,
            limit=limit,
            map_summary=summary.get("map"),
        ),
    }


def save_identity_context(summary: dict) -> dict[int, dict]:
    identities: dict[int, dict] = {}
    for player in summary.get("major_players", []):
        slot = player.get("slot")
        if slot is not None:
            identities[int(slot)] = save_player_context(player)
    for city_state in summary.get("active_city_states", []):
        slot = city_state.get("slot")
        if slot is not None:
            identities[int(slot)] = save_city_state_context(city_state)
    return identities


def save_player_context(player: dict) -> dict:
    civ = player.get("civilization")
    leader = player.get("leader")
    name = player.get("player_name")
    civ_zh = zh_name(civ)
    return {
        "slot": player.get("slot"),
        "player_name": name,
        "civilization": civ,
        "civilization_token": player.get("civilization_token"),
        "civilization_zh": zh_name(civ),
        "leader": leader,
        "leader_token": player.get("leader_token"),
        "leader_zh": zh_name(leader),
        "team": player.get("team"),
        "team_label": team_label(player.get("team")),
        "display": f"{civ} ({name})" if name else civ,
        "display_zh": f"{civ_zh}\uff08{name}\uff09" if name else civ_zh,
    }


def save_city_state_context(player: dict) -> dict:
    civ = player.get("civilization")
    city_state_type, city_state_type_zh = CITY_STATE_TYPES.get(civ, ("Unknown", "\u672a\u77e5"))
    return {
        "slot": player.get("slot"),
        "name": civ,
        "name_zh": zh_name(civ),
        "civilization": civ,
        "civilization_token": player.get("civilization_token"),
        "type": city_state_type,
        "type_zh": city_state_type_zh,
        "team": player.get("team"),
        "team_label": team_label(player.get("team")),
        "display": civ,
        "display_zh": zh_name(civ),
    }


def save_teams_llm_context(teams: dict) -> dict:
    return {
        "detected_format": teams.get("detected_format"),
        "teams": [
            {
                "team": team.get("team"),
                "team_label": team_label(team.get("team")),
                "size": team.get("size"),
                "players": [save_player_context(player) for player in team.get("players", [])],
            }
            for team in teams.get("teams", [])
        ],
    }


def save_map_context(map_summary: dict | None, save_identities: dict[int, dict], limit: int) -> dict | None:
    if not map_summary:
        return map_summary
    if map_summary.get("error"):
        return map_summary
    owned = []
    for item in map_summary.get("owned_tile_summary", [])[:limit]:
        slot = item.get("slot")
        owned.append(
            {
                **item,
                "player": save_slot_label(save_identities, slot),
                "player_zh": save_slot_label_zh(save_identities, slot),
            }
        )
    wonders = []
    for item in map_summary.get("wonders_on_map", [])[:limit]:
        asset = item.get("asset") or {}
        display = asset.get("display")
        wonders.append(
            {
                **item,
                "display_zh": zh_name(display),
                "owner_player": save_slot_label(save_identities, item.get("owner")),
                "owner_player_zh": save_slot_label_zh(save_identities, item.get("owner")),
            }
        )
    return {
        "source": map_summary.get("source"),
        "width": map_summary.get("width"),
        "height": map_summary.get("height"),
        "tile_count": map_summary.get("tile_count"),
        "owned_tile_summary": owned,
        "wonders_on_map": wonders,
        "top_resources": map_summary.get("top_resources", [])[:limit],
        "top_improvements": map_summary.get("top_improvements", [])[:limit],
    }


def save_player_details_context(
    paths: Civ6Paths,
    details: dict | None,
    save_identities: dict[int, dict],
    major_slots: set[int],
    limit: int,
    map_summary: dict | None = None,
) -> dict | None:
    if not details:
        return details
    if details.get("error"):
        return details
    players = []
    raw_major_players = [
        player for player in details.get("players", [])
        if player.get("slot") in major_slots
    ]
    raw_location_context = save_player_location_context(
        paths,
        raw_major_players,
        save_identities,
        map_summary,
        limit=limit,
    )

    for player in raw_major_players:
        slot = player.get("slot")
        cities = [
            {
                "id": city.get("id"),
                "name": city_name_zh(paths, city.get("raw_name"), city.get("display_name")),
                "name_zh": city_name_zh(paths, city.get("raw_name"), city.get("display_name")),
                "x": city.get("x"),
                "y": city.get("y"),
                "population": city.get("population"),
                "current_production": [
                    {
                        "type": item.get("production_type"),
                        "type_zh": PRODUCTION_TYPE_ZH.get(item.get("production_type")),
                        "item": item.get("display"),
                        "item_zh": DISTRICT_ZH.get(item.get("display")) or zh_name(item.get("display")),
                    }
                    for item in city.get("current_production", [])
                ],
            }
            for city in player.get("cities", [])
        ]
        district_groups = save_district_groups(paths, player.get("districts", []), limit=limit)
        players.append(
            {
                "slot": slot,
                "player": save_slot_label(save_identities, slot),
                "player_zh": save_slot_label_zh(save_identities, slot),
                "team": save_identities.get(slot, {}).get("team"),
                "team_label": save_identities.get(slot, {}).get("team_label"),
                "city_count": player.get("city_count"),
                "district_count": player.get("district_count"),
                "location": raw_location_context.get(slot),
                "cities": cities[: min(max(limit, 10), 12)],
                "districts_by_type": district_groups,
            }
        )
    return {
        "source": details.get("source"),
        "coordinate_orientation": {
            "x_axis": "\u6570\u503c\u8d8a\u5927\u8d8a\u9760\u4e1c",
            "y_axis": "\u6570\u503c\u8d8a\u5927\u8d8a\u9760\u5317\uff0c\u6570\u503c\u8d8a\u5c0f\u8d8a\u9760\u5357",
            "answering_hint": "\u56de\u7b54\u4f4d\u7f6e\u95ee\u9898\u65f6\u4f18\u5148\u4f7f\u7528 location.nearby_empire_centers\uff0c\u4e0d\u8981\u81ea\u884c\u628a y \u8f74\u5f53\u6210\u5357\u5411\u3002",
        },
        "players": players,
        "errors": details.get("errors", []),
    }


def save_district_groups(paths: Civ6Paths, districts: list[dict], limit: int) -> list[dict]:
    grouped: dict[str, dict] = {}
    sample_limit = 3
    city_limit = min(max(limit, 10), 20)
    for district in districts:
        display = district.get("display")
        group = save_district_group(display)
        if group == "City Center":
            continue
        entry = grouped.setdefault(
            group,
            {
                "district_group": group,
                "district_group_zh": DISTRICT_ZH.get(group) or zh_name(group),
                "count": 0,
                "built_count": 0,
                "cities": [],
                "districts": [],
            },
        )
        entry["count"] += 1
        if district.get("built"):
            entry["built_count"] += 1
        city_name = district.get("city")
        city_zh = city_name_zh(paths, district.get("city_raw_name"), city_name)
        if city_zh and len(entry["cities"]) < city_limit and all(item.get("name") != city_zh for item in entry["cities"]):
            entry["cities"].append(
                {
                    "name": city_zh,
                    "name_zh": city_zh,
                    "status": "built" if district.get("built") else "under_construction",
                    "status_zh": "\u5df2\u5b8c\u6210" if district.get("built") else "\u5efa\u9020\u4e2d",
                }
            )
        if len(entry["districts"]) < sample_limit:
            entry["districts"].append(
                {
                    "district": display,
                    "district_zh": DISTRICT_ZH.get(display) or zh_name(display),
                    "city": city_name_zh(paths, district.get("city_raw_name"), district.get("city")),
                    "city_zh": city_name_zh(paths, district.get("city_raw_name"), district.get("city")),
                    "x": district.get("x"),
                    "y": district.get("y"),
                    "built": bool(district.get("built")),
                    "pillaged": bool(district.get("pillaged")),
                }
            )
    return sorted(grouped.values(), key=lambda item: (-item["count"], item["district_group"]))


def save_player_location_context(
    paths: Civ6Paths,
    players: list[dict],
    save_identities: dict[int, dict],
    map_summary: dict | None,
    limit: int,
) -> dict[int, dict]:
    centers: dict[int, dict] = {}
    width = map_summary.get("width") if isinstance(map_summary, dict) else None
    height = map_summary.get("height") if isinstance(map_summary, dict) else None
    city_ownership = current_city_ownership_by_tag(players)
    for player in players:
        slot = player.get("slot")
        if slot is None:
            continue
        cities = [
            city for city in player.get("cities", [])
            if isinstance(city.get("x"), int) and isinstance(city.get("y"), int)
        ]
        if not cities:
            continue
        geometric_x = sum(city["x"] for city in cities) / len(cities)
        geometric_y = sum(city["y"] for city in cities) / len(cities)
        initial_x, initial_y = weighted_city_center(cities)
        city_roles = classify_city_location_roles(paths, cities, initial_x, initial_y)
        core_cities = [
            city for city, role in zip(cities, city_roles)
            if not role["likely_backline_or_outpost"]
        ] or cities
        identity = save_identities.get(slot, {})
        capital = player_capital_context(paths, player, slot, save_identities, city_ownership)
        current_core_x, current_core_y = weighted_city_center(core_cities)
        if capital.get("x") is not None and capital.get("y") is not None:
            center_x = float(capital["x"])
            center_y = float(capital["y"])
            center_method = (
                "\u539f\u59cb\u9996\u90fd/\u5b58\u6863 city id 0"
                if capital.get("currently_owned")
                else "\u539f\u59cb\u9996\u90fd\u5df2\u88ab\u522b\u4eba\u5360\u9886\uff0c\u4ecd\u7528\u4e8e\u51fa\u751f\u5730\u7f18"
            )
        else:
            center_x, center_y = current_core_x, current_core_y
            center_method = "\u539f\u59cb\u9996\u90fd\u672a\u89c2\u6d4b\uff0c\u6539\u7528\u5f53\u524d\u6838\u5fc3\u57ce\u5e02"
        centers[slot] = {
            "slot": slot,
            "player": save_slot_label(save_identities, slot),
            "player_zh": save_slot_label_zh(save_identities, slot),
            "team": identity.get("team"),
            "team_label": identity.get("team_label"),
            "center_x": round(center_x, 1),
            "center_y": round(center_y, 1),
            "current_core_center_x": round(current_core_x, 1),
            "current_core_center_y": round(current_core_y, 1),
            "geometric_center_x": round(geometric_x, 1),
            "geometric_center_y": round(geometric_y, 1),
            "map_region": map_region_zh(center_x, center_y, width, height),
            "current_core_map_region": map_region_zh(current_core_x, current_core_y, width, height),
            "capital": capital,
            "center_method": center_method,
            "cities": core_cities,
            "all_cities": cities,
            "city_roles": city_roles,
        }

    context: dict[int, dict] = {}
    neighbor_limit = min(max(limit, 4), 6)
    for slot, center in centers.items():
        center_neighbors = []
        city_neighbors = []
        for other_slot, other in centers.items():
            if other_slot == slot:
                continue
            dx = other["center_x"] - center["center_x"]
            dy = other["center_y"] - center["center_y"]
            distance = civ6_hex_distance(center["center_x"], center["center_y"], other["center_x"], other["center_y"], width)
            center_neighbors.append(
                {
                    "player": other["player"],
                    "player_zh": other["player_zh"],
                    "team_relation": team_relation_zh(center.get("team"), other.get("team")),
                    "direction": direction_zh(dx, dy),
                    "distance": distance,
                    "distance_unit": "hexes",
                    "center_x": other["center_x"],
                    "center_y": other["center_y"],
                    "map_region": other["map_region"],
                }
            )
            nearest = nearest_city_pair(paths, center["cities"], other["cities"], width)
            if nearest is not None:
                city_neighbors.append(
                    {
                        "player": other["player"],
                        "player_zh": other["player_zh"],
                        "team_relation": team_relation_zh(center.get("team"), other.get("team")),
                        **nearest,
                    }
                )
        center_neighbors.sort(key=lambda item: item["distance"])
        city_neighbors.sort(key=lambda item: item["distance"])
        allied_centers = [item for item in center_neighbors if item["team_relation"] == "\u540c\u961f"]
        opponent_centers = [item for item in center_neighbors if item["team_relation"] == "\u5bf9\u624b"]
        context[slot] = {
            "center": {
                "x": center["center_x"],
                "y": center["center_y"],
                "map_region": center["map_region"],
                "method": center["center_method"],
                "distance_unit": "hexes",
            },
            "current_core_center": {
                "x": center["current_core_center_x"],
                "y": center["current_core_center_y"],
                "map_region": center["current_core_map_region"],
                "method": "\u5f53\u524d\u4ecd\u6301\u6709\u7684\u6838\u5fc3\u57ce\u5e02\uff0c\u6392\u9664\u8dd1\u79fb\u6c11/\u4fdd\u547d\u98de\u5730",
            },
            "original_capital": center["capital"],
            "capital": center["capital"],
            "likely_backline_or_outpost_cities": [
                role for role in center["city_roles"]
                if role["likely_backline_or_outpost"]
            ],
            "nearby_empire_centers": center_neighbors[:neighbor_limit],
            "nearby_allied_centers": allied_centers[:neighbor_limit],
            "nearby_opponent_centers": opponent_centers[:neighbor_limit],
            "nearest_city_pairs": city_neighbors[:neighbor_limit],
            "location_hint_zh": "\u5bbd\u6cdb\u95ee\u201c\u5468\u56f4/\u9644\u8fd1\u201d\u65f6\u4f18\u5148\u770b original_capital \u4e0e nearby_empire_centers\uff1bcenter \u8868\u793a\u539f\u59cb\u9996\u90fd/\u51fa\u751f\u5730\u7f18\uff0c\u4e0d\u662f\u4eba\u53e3\u52a0\u6743\u4e2d\u5fc3\uff1b\u82e5\u95ee\u8be5\u73a9\u5bb6\u5f53\u524d\u5269\u4e0b\u7684\u57ce\u5e02\u4f4d\u7f6e\uff0c\u6539\u770b current_core_center\u3002\u540c\u961f\u540e\u65b9\u98de\u5730\u5e38\u662f\u8dd1\u79fb\u6c11\u4fdd\u547d\u57ce\uff0c\u4e0d\u8981\u5f53\u6210\u771f\u5b9e\u4e3b\u6218\u7ebf\u3002nearest_city_pairs \u66f4\u50cf\u8fb9\u5883\u63a5\u89e6\u70b9\u3002",
        }
    return context


def weighted_city_center(cities: list[dict]) -> tuple[float, float]:
    total_weight = 0.0
    weighted_x = 0.0
    weighted_y = 0.0
    for city in cities:
        population = city.get("population")
        weight = float(population if isinstance(population, int) and population > 0 else 1)
        total_weight += weight
        weighted_x += city["x"] * weight
        weighted_y += city["y"] * weight
    if total_weight <= 0:
        return (0.0, 0.0)
    return (weighted_x / total_weight, weighted_y / total_weight)


def civ6_hex_distance(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    width: int | None = None,
) -> int:
    start_x = int(round(x1))
    start_y = int(round(y1))
    target_x = int(round(x2))
    target_y = int(round(y2))
    candidates = [target_x]
    if width:
        candidates.extend([target_x - width, target_x + width])
    return min(
        odd_r_hex_distance_no_wrap(start_x, start_y, candidate_x, target_y)
        for candidate_x in candidates
    )


def odd_r_hex_distance_no_wrap(x1: int, y1: int, x2: int, y2: int) -> int:
    q1, r1, s1 = odd_r_to_cube(x1, y1)
    q2, r2, s2 = odd_r_to_cube(x2, y2)
    return max(abs(q1 - q2), abs(r1 - r2), abs(s1 - s2))


def odd_r_to_cube(x: int, y: int) -> tuple[int, int, int]:
    q = x - (y - (y & 1)) // 2
    r = y
    return q, r, -q - r


def current_city_ownership_by_tag(players: list[dict]) -> dict[str, dict]:
    ownership = {}
    for player in players:
        slot = player.get("slot")
        for city in player.get("cities", []):
            raw_name = city.get("raw_name")
            if raw_name:
                ownership[raw_name] = {
                    "slot": slot,
                    "city": city,
                }
    return ownership


def player_capital_context(
    paths: Civ6Paths,
    player: dict,
    slot: int,
    save_identities: dict[int, dict],
    city_ownership: dict[str, dict],
) -> dict:
    identity = save_identities.get(slot, {})
    owned_capital = next((city for city in player.get("cities", []) if city.get("id") == 0), None)
    if owned_capital is not None:
        name = city_name_zh(paths, owned_capital.get("raw_name"), owned_capital.get("display_name"))
        return {
            "name": name,
            "name_zh": name,
            "x": owned_capital.get("x"),
            "y": owned_capital.get("y"),
            "population": owned_capital.get("population"),
            "currently_owned": True,
            "source": "\u5b58\u6863\u57ce\u5e02 id 0",
        }

    expected_tag = first_city_name_tag(paths.cache_dir, identity.get("civilization_token") or identity.get("civilization"))
    expected_name = city_name_zh(paths, expected_tag, None) if expected_tag else None
    owner = city_ownership.get(expected_tag) if expected_tag else None
    observed_owner = owner.get("slot") if owner else None
    observed_city = owner.get("city") if owner else None
    return {
        "name": expected_name,
        "name_zh": expected_name,
        "expected_loc_tag": expected_tag,
        "x": observed_city.get("x") if observed_city else None,
        "y": observed_city.get("y") if observed_city else None,
        "population": observed_city.get("population") if observed_city else None,
        "currently_owned": False,
        "status_zh": "\u539f\u59cb\u9996\u90fd\u4e0d\u5728\u8be5\u73a9\u5bb6\u5f53\u524d\u57ce\u5e02\u5217\u8868\u4e2d",
        "observed_current_owner": save_slot_label(save_identities, observed_owner),
        "observed_current_owner_zh": save_slot_label_zh(save_identities, observed_owner),
        "source": "\u57ce\u5e02 id 0 \u7f3a\u5931\uff0c\u4f7f\u7528 CityNames \u9996\u4e2a\u57ce\u540d\u56de\u67e5",
    }


@lru_cache(maxsize=512)
def first_city_name_tag(cache_dir: Path, civilization: str | None) -> str | None:
    if not civilization:
        return None
    db_path = cache_dir / "DebugGameplay.sqlite"
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                "select CityName from CityNames where CivilizationType = ? order by SortIndex, ID limit 1",
                (civilization,),
            ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row and row[0] else None


def classify_city_location_roles(
    paths: Civ6Paths,
    cities: list[dict],
    center_x: float,
    center_y: float,
) -> list[dict]:
    if not cities:
        return []
    max_population = max(
        (city.get("population") for city in cities if isinstance(city.get("population"), int)),
        default=1,
    )
    roles = []
    for city in cities:
        dx = city["x"] - center_x
        dy = city["y"] - center_y
        distance = civ6_hex_distance(city["x"], city["y"], center_x, center_y)
        population = city.get("population")
        is_small = isinstance(population, int) and population <= max(5, max_population * 0.5)
        likely_outpost = len(cities) > 1 and is_small and distance >= 12
        city_name = city_name_zh(paths, city.get("raw_name"), city.get("display_name"))
        roles.append(
            {
                "city": city_name,
                "city_zh": city_name,
                "x": city.get("x"),
                "y": city.get("y"),
                "population": population,
                "direction_from_core": direction_zh(dx, dy),
                "distance_from_core": round(distance, 1),
                "likely_backline_or_outpost": likely_outpost,
                "interpretation_zh": "\u53ef\u80fd\u662f\u8dd1\u79fb\u6c11/\u961f\u53cb\u540e\u65b9\u4fdd\u547d\u98de\u5730"
                if likely_outpost
                else "\u6838\u5fc3\u6216\u8fd1\u6838\u5fc3\u57ce\u5e02",
            }
        )
    return roles


def team_relation_zh(team: int | None, other_team: int | None) -> str:
    if team is None or other_team is None:
        return "\u672a\u77e5"
    if team == other_team:
        return "\u540c\u961f"
    return "\u5bf9\u624b"


def nearest_city_pair(
    paths: Civ6Paths,
    source_cities: list[dict],
    target_cities: list[dict],
    map_width: int | None = None,
) -> dict | None:
    best: tuple[int, dict, dict] | None = None
    for source in source_cities:
        for target in target_cities:
            dx = target["x"] - source["x"]
            dy = target["y"] - source["y"]
            distance = civ6_hex_distance(source["x"], source["y"], target["x"], target["y"], map_width)
            if best is None or distance < best[0]:
                best = (distance, source, target)
    if best is None:
        return None
    distance, source, target = best
    dx = target["x"] - source["x"]
    dy = target["y"] - source["y"]
    source_name = city_name_zh(paths, source.get("raw_name"), source.get("display_name"))
    target_name = city_name_zh(paths, target.get("raw_name"), target.get("display_name"))
    return {
        "direction": direction_zh(dx, dy),
        "distance": distance,
        "distance_unit": "hexes",
        "from_city": source_name,
        "from_city_zh": source_name,
        "to_city": target_name,
        "to_city_zh": target_name,
    }


def map_region_zh(x: float, y: float, width: int | None, height: int | None) -> str | None:
    if not width or not height:
        return None
    west_boundary = width * 0.4
    east_boundary = width * 0.6
    south_boundary = height * 0.4
    north_boundary = height * 0.6
    horizontal = "\u897f" if x < west_boundary else "\u4e1c" if x > east_boundary else ""
    vertical = "\u5317" if y > north_boundary else "\u5357" if y < south_boundary else ""
    return f"{horizontal}{vertical}" or "\u4e2d\u90e8"


def direction_zh(dx: float, dy: float) -> str:
    distance = math.hypot(dx, dy)
    if distance < 1:
        return "\u9644\u8fd1"
    threshold = max(distance * 0.35, 1.5)
    horizontal = "\u4e1c" if dx > threshold else "\u897f" if dx < -threshold else ""
    vertical = "\u5317" if dy > threshold else "\u5357" if dy < -threshold else ""
    return f"{horizontal}{vertical}" or "\u9644\u8fd1"


def save_district_group(display: str | None) -> str:
    groups = {
        "Seowon": "Campus",
        "Observatory": "Campus",
        "Campus": "Campus",
        "Theater": "Theater Square",
        "DIPLOMATIC QUARTER": "Diplomatic Quarter",
        "Government": "Government Plaza",
        "0x62f1b509": "Wonder",
    }
    return groups.get(display or "", display or "Unknown")


def team_context(snapshot: GameSnapshot, rows: list[PlayerTurnStats]) -> dict:
    grouped: dict[int, list[PlayerTurnStats]] = defaultdict(list)
    for stats in rows:
        identity = snapshot.identity_for(stats.slot)
        if identity is None or identity.team is None:
            continue
        grouped[identity.team].append(stats)

    teams = []
    for team, members in sorted(grouped.items()):
        ordered_members = sorted(members, key=lambda stats: stats.slot if stats.slot is not None else 9999)
        teams.append(
            {
                "team": team,
                "team_label": team_label(team),
                "size": len(ordered_members),
                "players": [
                    {
                        "slot": stats.slot,
                        "player": player_label(snapshot, stats.slot),
                        "player_zh": player_label_zh(snapshot, stats.slot),
                        "civilization": snapshot.identity_for(stats.slot).short_civ
                        if snapshot.identity_for(stats.slot)
                        else None,
                        "civilization_zh": zh_name(snapshot.identity_for(stats.slot).short_civ)
                        if snapshot.identity_for(stats.slot)
                        else None,
                    }
                    for stats in ordered_members
                ],
            }
        )

    return {
        "detected_format": "v".join(str(team["size"]) for team in teams) if teams else None,
        "teams": teams,
    }


def leader_context(snapshot: GameSnapshot, rows: list[PlayerTurnStats]) -> dict:
    metrics = {
        "score": "score",
        "production_per_turn": "production_yield",
        "science_per_turn": "science_yield",
        "culture_per_turn": "culture_yield",
        "tourism": "tourism",
        "gold_balance": "gold_balance",
        "gold_per_turn": "gold_yield",
        "faith_per_turn": "faith_yield",
        "techs": "techs",
        "civics": "civics",
        "cities": "cities",
        "population": "population",
    }
    leaders: dict[str, dict] = {}
    for label, attr in metrics.items():
        ranked = sorted(
            [(stats, getattr(stats, attr, None)) for stats in rows if getattr(stats, attr, None) is not None],
            key=lambda item: item[1],
            reverse=True,
        )
        if not ranked:
            continue
        stats, value = ranked[0]
        leaders[label] = {
            "player": describe_player(snapshot, stats),
            "player_zh": player_label_zh(snapshot, stats.slot),
            "value": value,
        }
    return leaders


def victory_context(snapshot: GameSnapshot, rows: list[PlayerTurnStats]) -> list[dict]:
    entries = []
    for stats in rows:
        entries.append(
            {
                "player": describe_player(snapshot, stats),
                "player_zh": player_label_zh(snapshot, stats.slot),
                "score": stats.score,
                "science_proxy": {
                    "techs": stats.techs,
                    "science_per_turn": stats.science_yield,
                    "tech_score": stats.score_categories.get("CATEGORY_TECH"),
                },
                "culture_proxy": {
                    "tourism": stats.tourism,
                    "culture_per_turn": stats.culture_yield,
                    "civics": stats.civics,
                    "civic_score": stats.score_categories.get("CATEGORY_CIVICS"),
                },
                "religion_proxy": {
                    "faith_per_turn": stats.faith_yield,
                    "faith_balance": stats.faith_balance,
                    "religion_score": stats.score_categories.get("CATEGORY_RELIGION"),
                },
                "diplomatic_proxy": {
                    "diplo_victory_points": stats.diplo_victory_points,
                },
                "wonder_proxy": {
                    "wonder_score": stats.score_categories.get("CATEGORY_WONDER"),
                },
            }
        )
    return entries


def pantheon_context(paths: Civ6Paths, snapshot: GameSnapshot) -> list[dict]:
    path = paths.logs_dir / "net_message_debug.log"
    if not path.exists():
        return []

    seen: set[tuple[str, int, str]] = set()
    pantheons = []
    for line in latest_text_session_lines(path):
        if "FOUND_PANTHEON" not in line:
            continue
        match = PANTHEON_RE.search(line)
        if not match:
            continue
        timestamp = match.group("time")
        slot = parse_int(match.group("player"))
        belief = format_game_token(match.group("belief"))
        key_slot = slot if slot is not None else -1
        key = (timestamp, key_slot, belief)
        if key in seen:
            continue
        seen.add(key)
        pantheons.append(
            {
                "timestamp": timestamp,
                "player": player_label(snapshot, slot),
                "player_zh": player_label_zh(snapshot, slot),
                "slot": slot,
                "pantheon": belief,
                "pantheon_zh": zh_name(belief),
                "source": path.name,
            }
        )
    return pantheons


def great_people_context(paths: Civ6Paths, snapshot: GameSnapshot, limit: int) -> list[dict]:
    rows = latest_session_csv_dicts(paths.logs_dir / "Game_GreatPeople.csv", "Turn")
    events = []
    last_recipient_by_person: dict[str, int] = {}
    last_era_by_person: dict[str, str] = {}
    for row in rows:
        event = row.get("Event", "").strip()
        recipient = parse_int(row.get("Recipient Player", ""))
        if event.lower() == "added to present timeline" and (recipient is None or recipient < 0):
            continue
        turn = parse_int(row.get("Turn", ""))
        person = format_game_token(row.get("GP Individual", ""))
        era = format_game_token(row.get("GP Era", ""))
        if (recipient is None or recipient < 0) and person in last_recipient_by_person:
            recipient = last_recipient_by_person[person]
        if era == "Era tbd" and person in last_era_by_person:
            era = last_era_by_person[person]
        events.append(
            {
                "turn": turn,
                "turn_label": turn_label(turn),
                "event": event,
                "great_person": person,
                "great_person_zh": zh_name(person),
                "class": format_game_token(row.get("GP Class", "")),
                "class_zh": zh_name(format_game_token(row.get("GP Class", ""))),
                "era": era,
                "era_zh": zh_name(era),
                "recipient": player_label(snapshot, recipient),
                "recipient_zh": player_label_zh(snapshot, recipient),
                "recipient_slot": recipient,
                "source": "Game_GreatPeople.csv",
            }
        )
        if person and recipient is not None and recipient >= 0:
            last_recipient_by_person[person] = recipient
        if person and era and era != "Era tbd":
            last_era_by_person[person] = era
    return events[-limit:]


def wonder_context(paths: Civ6Paths, snapshot: GameSnapshot, limit: int) -> dict:
    rows = latest_session_csv_dicts(paths.logs_dir / "City_BuildQueue.csv", "Game Turn")
    wonder_rows = [row for row in rows if row.get("Current Item", "") in DEFAULT_WONDER_TYPES]
    completed = []
    active_by_city: dict[tuple[str, str], dict] = {}
    owner_history = city_owner_history(paths)
    fallback_owners = city_owner_fallbacks(paths, snapshot)
    for row in wonder_rows:
        turn = parse_int(row.get("Game Turn", ""))
        city_key = row.get("City", "")
        owner = owner_for_city(city_key, turn, owner_history, fallback_owners)
        city = format_game_token(city_key)
        city_zh = city_name_zh(paths, None, city)
        wonder = format_game_token(row.get("Current Item", ""))
        current = parse_float(row.get("Current Production", ""))
        needed = parse_float(row.get("Production Needed", ""))
        entry = {
            "turn": turn,
            "turn_label": turn_label(turn),
            "city": city_zh,
            "city_zh": city_zh,
            "player": player_label(snapshot, owner),
            "player_zh": player_label_zh(snapshot, owner),
            "slot": owner,
            "wonder": wonder,
            "wonder_zh": zh_name(wonder),
            "current_production": current,
            "production_needed": needed,
            "source": "City_BuildQueue.csv",
        }
        if current is not None and needed is not None and current >= needed:
            completed.append(entry)
        active_by_city[(city, wonder)] = entry

    score_increases = wonder_score_increases(paths, snapshot)
    active = [
        entry for entry in active_by_city.values()
        if entry.get("turn") == snapshot.latest_turn
    ]
    active.sort(key=lambda item: (item["city"], item["wonder"]))

    return {
        "completed": completed[-limit:],
        "active_latest_turn": active,
        "score_increases": score_increases[-limit:],
    }


def wonder_score_increases(paths: Civ6Paths, snapshot: GameSnapshot) -> list[dict]:
    rows = latest_session_csv_dicts(paths.logs_dir / "Game_PlayerScores.csv", "Game Turn")
    by_player: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for row in rows:
        turn = parse_int(row.get("Game Turn", ""))
        slot = parse_int(row.get("Player", ""))
        value = parse_float(row.get("CATEGORY_WONDER", ""))
        if turn is None or slot is None or value is None:
            continue
        by_player[slot].append((turn, value))

    increases = []
    for slot, values in by_player.items():
        previous: float | None = None
        for turn, value in sorted(values):
            if previous is not None and value > previous:
                increases.append(
                    {
                        "turn": turn,
                        "turn_label": turn_label(turn),
                        "player": player_label(snapshot, slot),
                        "player_zh": player_label_zh(snapshot, slot),
                        "slot": slot,
                        "delta": value - previous,
                        "total_wonder_score": value,
                        "source": "Game_PlayerScores.csv",
                    }
                )
            previous = value
    return sorted(increases, key=lambda item: (item["turn"], item["slot"]))


def districts_and_builds_context(
    paths: Civ6Paths,
    snapshot: GameSnapshot,
    rows: list[PlayerTurnStats],
    limit: int,
) -> dict:
    build_rows = current_game_csv_dicts(paths.logs_dir / "City_BuildQueue.csv", "Game Turn", snapshot.latest_turn)
    if not build_rows:
        return {"known_completed_districts": [], "active_latest_turn": []}

    major_slots = {stats.slot for stats in rows if stats.slot is not None}
    owner_history = city_owner_history(paths)
    fallback_owners = city_owner_fallbacks(paths, snapshot)

    completed_by_player: dict[int, dict[str, dict]] = defaultdict(dict)
    completed_seen: set[tuple[int, str, str]] = set()
    for row in build_rows:
        item = row.get("Current Item", "")
        if not item.startswith("DISTRICT_"):
            continue
        turn = parse_int(row.get("Game Turn", ""))
        city_key = row.get("City", "")
        owner = owner_for_city(city_key, turn, owner_history, fallback_owners)
        if owner is None or owner not in major_slots:
            continue
        current = parse_float(row.get("Current Production", ""))
        needed = parse_float(row.get("Production Needed", ""))
        if current is None or needed is None or current < needed:
            continue
        unique_key = (owner, city_key, item)
        if unique_key in completed_seen:
            continue
        completed_seen.add(unique_key)
        district = district_name(item)
        group = DISTRICT_GROUPS.get(item, district)
        player_entry = completed_by_player[owner]
        district_entry = player_entry.setdefault(
            group,
            {
                "district_group": group,
                "district_group_zh": DISTRICT_ZH.get(group),
                "count": 0,
                "cities": [],
                "latest_turn": turn,
                "latest_turn_label": turn_label(turn),
            },
        )
        district_entry["count"] += 1
        city_display = format_game_token(city_key)
        city_zh = city_name_zh(paths, None, city_display)
        district_entry["cities"].append(
            {
                "city": city_zh,
                "city_zh": city_zh,
                "district": district,
                "district_zh": DISTRICT_ZH.get(district),
                "completed_turn": turn,
                "completed_turn_label": turn_label(turn),
            }
        )
        if turn is not None and (district_entry["latest_turn"] is None or turn > district_entry["latest_turn"]):
            district_entry["latest_turn"] = turn
            district_entry["latest_turn_label"] = turn_label(turn)

    completed = []
    for slot, districts in sorted(completed_by_player.items()):
        ordered_districts = sorted(districts.values(), key=lambda item: (-item["count"], item["district_group"]))
        completed.append(
            {
                "player": player_label(snapshot, slot),
                "player_zh": player_label_zh(snapshot, slot),
                "slot": slot,
                "districts": ordered_districts,
            }
        )

    latest_turn = max((parse_int(row.get("Game Turn", "")) or 0) for row in build_rows)
    active = []
    for row in build_rows:
        turn = parse_int(row.get("Game Turn", ""))
        if turn != latest_turn:
            continue
        city_key = row.get("City", "")
        owner = owner_for_city(city_key, turn, owner_history, fallback_owners)
        if owner is None or owner not in major_slots:
            continue
        item = row.get("Current Item", "")
        current = parse_float(row.get("Current Production", ""))
        needed = parse_float(row.get("Production Needed", ""))
        active.append(
            {
                "turn": turn,
                "turn_label": turn_label(turn),
                "player": player_label(snapshot, owner),
                "player_zh": player_label_zh(snapshot, owner),
                "slot": owner,
                "city": city_name_zh(paths, None, format_game_token(city_key)),
                "city_zh": city_name_zh(paths, None, format_game_token(city_key)),
                "item": district_name(item) if item.startswith("DISTRICT_") else format_game_token(item),
                "item_zh": DISTRICT_ZH.get(district_name(item)) if item.startswith("DISTRICT_") else zh_name(format_game_token(item)),
                "current_production": current,
                "production_needed": needed,
                "completed_or_overflowed": current is not None and needed is not None and current >= needed,
            }
        )

    active.sort(key=lambda item: (item["slot"], item["city"], item["item"]))
    return {
        "source": "City_BuildQueue.csv with AI_CityBuild.csv city-owner history",
        "latest_build_turn": latest_turn,
        "latest_build_turn_label": turn_label(latest_turn),
        "known_completed_districts": completed,
        "active_latest_turn": active[:limit],
    }


def governors_context(paths: Civ6Paths, snapshot: GameSnapshot, limit: int) -> dict:
    rows = latest_session_csv_dicts(paths.logs_dir / "Governors.csv", "Turn")
    if not rows:
        return {"recent_events": [], "assignments": []}

    slot_by_civ = {
        identity.civilization: slot
        for slot, identity in snapshot.players.items()
        if identity.civilization
    }
    events = []
    assignments: dict[tuple[int, str], dict] = {}
    for row in rows:
        turn = parse_int(row.get("Turn", ""))
        owner = row.get("Owner", "")
        slot = slot_by_civ.get(owner)
        if slot is None:
            continue
        governor = governor_name(row.get("GovernorName", ""))
        event = row.get("EventType", "").strip()
        raw_city = row.get("City", "")
        city = format_game_token(raw_city)
        city_zh = city_name_zh(paths, None, city)
        entry = {
            "turn": turn,
            "turn_label": turn_label(turn),
            "event": event,
            "player": player_label(snapshot, slot),
            "player_zh": player_label_zh(snapshot, slot),
            "slot": slot,
            "governor": governor,
            "governor_zh": GOVERNOR_ZH.get(governor),
            "city": None if raw_city == "NO CITY" else city_zh,
            "city_zh": None if raw_city == "NO CITY" else city_zh,
        }
        events.append(entry)
        if event == "Governor Assigned" and raw_city != "NO CITY":
            assignments[(slot, governor)] = entry

    return {
        "recent_events": events[-limit:],
        "assignments": sorted(assignments.values(), key=lambda item: (item["slot"], item["governor"])),
    }


def city_owner_history(paths: Civ6Paths) -> dict[str, list[tuple[int, int]]]:
    rows = current_game_csv_dicts(paths.logs_dir / "AI_CityBuild.csv", "Game Turn", None)
    history: dict[str, list[tuple[int, int]]] = defaultdict(list)
    seen: set[tuple[str, int, int]] = set()
    for row in rows:
        turn = parse_int(row.get("Game Turn", ""))
        slot = parse_int(row.get("Player", ""))
        city = row.get("City", "")
        if turn is None or slot is None or not city:
            continue
        key = (city, turn, slot)
        if key in seen:
            continue
        seen.add(key)
        history[city].append((turn, slot))
    for city in history:
        history[city].sort()
    return history


def current_game_csv_dicts(path, turn_column: str, latest_turn: int | None) -> list[dict[str, str]]:
    rows = list(read_csv_dicts(path) or [])
    start = 0
    previous_turn: int | None = None
    for index, row in enumerate(rows):
        turn = parse_int(row.get(turn_column, ""))
        if turn is None:
            continue
        if turn <= 2 and (previous_turn is None or previous_turn > 20):
            start = index
        previous_turn = turn
    selected = rows[start:]
    if latest_turn is None:
        return selected
    return [
        row for row in selected
        if (parse_int(row.get(turn_column, "")) or -1) <= latest_turn
    ]


def city_owner_fallbacks(paths: Civ6Paths, snapshot: GameSnapshot) -> dict[str, int]:
    db_path = paths.cache_dir / "DebugGameplay.sqlite"
    if not db_path.exists():
        return {}
    slot_by_civ = {
        identity.civilization: slot
        for slot, identity in snapshot.players.items()
        if identity.civilization
    }
    try:
        with sqlite3.connect(db_path) as connection:
            rows = connection.execute("select CityName, CivilizationType from CityNames").fetchall()
    except sqlite3.Error:
        return {}
    owners: dict[str, int] = {}
    for city, civ in rows:
        slot = slot_by_civ.get(civ)
        if slot is not None:
            owners[str(city)] = slot
    return owners


def owner_for_city(
    city: str,
    turn: int | None,
    history: dict[str, list[tuple[int, int]]],
    fallback: dict[str, int],
) -> int | None:
    owner: int | None = None
    for owner_turn, slot in history.get(city, []):
        if turn is not None and owner_turn > turn:
            break
        owner = slot
    if owner is not None:
        return owner
    return fallback.get(city)


def district_name(value: str) -> str:
    return clean_game_name(value, "DISTRICT_")


def governor_name(value: str) -> str:
    text = value.strip()
    if text.startswith("LOC_GOVERNOR_") and text.endswith("_NAME"):
        text = text[len("LOC_GOVERNOR_"):-len("_NAME")]
    elif text.startswith("GOVERNOR_"):
        text = text[len("GOVERNOR_"):]
    return clean_game_name(text)


def gold_context(paths: Civ6Paths, snapshot: GameSnapshot) -> dict:
    path = paths.logs_dir / "DiplomacyDeals.log"
    if not path.exists():
        return overlay_gold_context(snapshot) or empty_gold_context()

    transfers = []
    current_turn: int | None = None
    current_deal: str | None = None
    for line in latest_deal_session_lines(path):
        turn_match = DEAL_TURN_RE.search(line)
        if turn_match:
            current_turn = parse_int(turn_match.group("turn"))
            current_deal = turn_match.group("deal")
            continue
        if "Enacting Deal Item" not in line:
            continue
        gold_match = DEAL_GOLD_RE.search(line)
        if not gold_match:
            continue
        from_slot = parse_int(gold_match.group("from"))
        to_slot = parse_int(gold_match.group("to"))
        amount = parse_float(gold_match.group("amount")) or 0.0
        duration = parse_int(gold_match.group("duration")) or 0
        timing = gold_timing(duration)
        transfers.append(
            {
                "turn": current_turn,
                "turn_label": turn_label(current_turn),
                "from": player_label(snapshot, from_slot),
                "from_zh": player_label_zh(snapshot, from_slot),
                "from_slot": from_slot,
                "to": player_label(snapshot, to_slot),
                "to_zh": player_label_zh(snapshot, to_slot),
                "to_slot": to_slot,
                "amount": amount,
                "duration": duration,
                "timing": timing,
                "amount_zh": gold_transfer_amount_zh(amount, duration, timing),
                "deal_id": current_deal,
                "source": path.name,
            }
        )
    if not transfers:
        return overlay_gold_context(snapshot) or empty_gold_context()
    return gold_payload_from_transfers(snapshot, transfers)


def empty_gold_context() -> dict:
    return {
        "available": False,
        "summary_zh": "当前未解析到玩家间金币转账记录。若观众问谁花钱最多，可用金币余额和每回合金币作经济体量估计。",
        "leader_zh": None,
        "recent_transfers_zh": [],
        "totals_sent_desc": [],
        "gpt_sent_desc": [],
        "transfers": [],
        "totals_sent": [],
        "gpt_sent": [],
    }


def overlay_gold_context(
    snapshot: GameSnapshot,
    overlay_json: Path = Path("obs/overlay.json"),
    news_text: Path = Path("obs/news.txt"),
) -> dict | None:
    transfers = []

    if overlay_json.exists():
        try:
            data = json.loads(overlay_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        items = data.get("news")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or "")
                transfer = gold_transfer_from_overlay_news(snapshot, text, item.get("icons"), str(overlay_json))
                if transfer is not None:
                    transfers.append(transfer)

    if news_text.exists():
        try:
            lines = news_text.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            transfer = gold_transfer_from_text_news(snapshot, line, str(news_text))
            if transfer is not None:
                transfers.append(transfer)

    if not transfers:
        return None
    deduped = []
    seen: set[tuple[object, object, object, object]] = set()
    for transfer in transfers:
        key = (
            transfer.get("turn"),
            transfer.get("from_slot"),
            transfer.get("to_slot"),
            transfer.get("amount"),
            transfer.get("timing"),
            transfer.get("duration"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(transfer)
    payload = gold_payload_from_transfers(snapshot, deduped)
    payload["source_note_zh"] = "金币统计来自 OBS 最近新闻缓存；适合回答直播画面里的近期转账。"
    return payload


def gold_transfer_from_overlay_news(
    snapshot: GameSnapshot,
    text: str,
    icons: object,
    source: str,
) -> dict | None:
    match = OVERLAY_GOLD_NEWS_RE.search(text)
    if not match:
        return gold_transfer_from_text_news(snapshot, text, source)
    icon_items = icons if isinstance(icons, list) else []
    if len(icon_items) < 2 or not isinstance(icon_items[0], dict) or not isinstance(icon_items[1], dict):
        return gold_transfer_from_text_news(snapshot, text, source)
    from_slot = parse_int(str(icon_items[0].get("slot")))
    to_slot = parse_int(str(icon_items[1].get("slot")))
    if from_slot is None:
        return None
    amount = parse_float(match.group("amount")) or 0.0
    turn = parse_int(match.group("turn"))
    duration = parse_int(match.group("duration")) or 0
    timing = "per_turn" if match.group("kind") == "回合金" else gold_timing(duration)
    return gold_transfer_payload(snapshot, turn, from_slot, to_slot, amount, source, duration=duration, timing=timing)


def gold_transfer_from_text_news(snapshot: GameSnapshot, text: str, source: str) -> dict | None:
    match = TEXT_GOLD_NEWS_RE.search(text)
    if not match:
        return None
    from_slot = slot_from_news_label(snapshot, match.group("from"))
    to_slot = slot_from_news_label(snapshot, match.group("to"))
    if from_slot is None:
        return None
    amount = parse_float(match.group("amount")) or 0.0
    turn = parse_int(match.group("turn"))
    duration = parse_int(match.group("duration")) or 0
    timing = "per_turn" if match.group("kind") == "回合金" else gold_timing(duration)
    return gold_transfer_payload(snapshot, turn, from_slot, to_slot, amount, source, duration=duration, timing=timing)


def gold_transfer_payload(
    snapshot: GameSnapshot,
    turn: int | None,
    from_slot: int | None,
    to_slot: int | None,
    amount: float,
    source: str,
    *,
    duration: int = 0,
    timing: str | None = None,
) -> dict:
    resolved_timing = timing or gold_timing(duration)
    return {
        "turn": turn,
        "turn_label": turn_label(turn),
        "from": player_label(snapshot, from_slot),
        "from_zh": player_label_zh(snapshot, from_slot),
        "from_slot": from_slot,
        "to": player_label(snapshot, to_slot),
        "to_zh": player_label_zh(snapshot, to_slot),
        "to_slot": to_slot,
        "amount": amount,
        "duration": duration,
        "timing": resolved_timing,
        "amount_zh": gold_transfer_amount_zh(amount, duration, resolved_timing),
        "deal_id": None,
        "source": source,
    }


def slot_from_news_label(snapshot: GameSnapshot, label: str) -> int | None:
    normalized = normalize_news_label(label)
    if not normalized:
        return None
    for slot, identity in snapshot.players.items():
        candidates = {
            identity.player_name or "",
            identity.network_name or "",
            identity.short_civ,
            identity.short_leader,
            zh_name(identity.short_civ) or "",
            zh_name(identity.short_leader) or "",
            player_label(snapshot, slot),
            player_label_zh(snapshot, slot),
        }
        for candidate in candidates:
            candidate_normalized = normalize_news_label(candidate)
            if candidate_normalized and candidate_normalized in normalized:
                return slot
    return None


def normalize_news_label(value: str | None) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\bP\d+\b", "", text)
    text = re.sub(r"[()\uff08\uff09,\uff0c\s]", "", text)
    return text.casefold()


def gold_timing(duration: int | None) -> str:
    return "per_turn" if (duration or 0) > 0 else "one_time"


def gold_transfer_amount_zh(amount: object, duration: int | None, timing: str | None) -> str:
    if timing == "per_turn":
        suffix = f"（持续 {duration}T）" if duration and duration > 0 else ""
        return f"{format_gold_amount(amount)} 回合金{suffix}"
    return f"{format_gold_amount(amount)} 金币"


def gold_transfer_sentence_zh(transfer: dict) -> str:
    return (
        f"{transfer.get('turn_label') or ''} "
        f"{transfer.get('from_zh')} 向 {transfer.get('to_zh')} 送出 "
        f"{transfer.get('amount_zh') or gold_transfer_amount_zh(transfer.get('amount'), parse_int(str(transfer.get('duration'))) or 0, transfer.get('timing'))}"
    ).strip()


def gold_payload_from_transfers(snapshot: GameSnapshot, transfers: list[dict]) -> dict:
    cash_totals: dict[int, float] = defaultdict(float)
    gpt_totals: dict[int, float] = defaultdict(float)
    for transfer in transfers:
        from_slot = parse_int(str(transfer.get("from_slot")))
        if from_slot is None:
            continue
        amount = parse_float(str(transfer.get("amount"))) or 0.0
        if transfer.get("timing") == "per_turn":
            gpt_totals[from_slot] += amount
        else:
            cash_totals[from_slot] += amount
    cash_totals_desc = [
        {
            "player": player_label(snapshot, slot),
            "player_zh": player_label_zh(snapshot, slot),
            "slot": slot,
            "total_gold_sent": amount,
            "total_gold_sent_zh": f"{format_gold_amount(amount)} 金币",
        }
        for slot, amount in sorted(cash_totals.items(), key=lambda item: item[1], reverse=True)
    ]
    gpt_totals_desc = [
        {
            "player": player_label(snapshot, slot),
            "player_zh": player_label_zh(snapshot, slot),
            "slot": slot,
            "total_gpt_sent": amount,
            "total_gpt_sent_zh": f"{format_gold_amount(amount)} 回合金",
        }
        for slot, amount in sorted(gpt_totals.items(), key=lambda item: item[1], reverse=True)
    ]
    recent_transfers_zh = [
        gold_transfer_sentence_zh(transfer)
        for transfer in transfers[-10:]
    ]
    cash_leader = cash_totals_desc[0] if cash_totals_desc else None
    gpt_leader = gpt_totals_desc[0] if gpt_totals_desc else None
    if cash_leader and gpt_leader:
        summary = (
            f"现金转账最多的是 {cash_leader['player_zh']}，共送出 {cash_leader['total_gold_sent_zh']}；"
            f"回合金最多的是 {gpt_leader['player_zh']}，共送出 {gpt_leader['total_gpt_sent_zh']}。"
        )
    elif cash_leader:
        summary = f"现金转账最多的是 {cash_leader['player_zh']}，共送出 {cash_leader['total_gold_sent_zh']}。"
    elif gpt_leader:
        summary = f"回合金转账最多的是 {gpt_leader['player_zh']}，共送出 {gpt_leader['total_gpt_sent_zh']}。"
    else:
        summary = "当前未记录到玩家间金币转账。若观众问谁花钱最多，可用金币余额和每回合金币作经济体量估计。"
    return {
        "available": bool(transfers),
        "summary_zh": summary,
        "leader_zh": cash_leader or gpt_leader,
        "cash_leader_zh": cash_leader,
        "gpt_leader_zh": gpt_leader,
        "recent_transfers_zh": recent_transfers_zh,
        "totals_sent_desc": cash_totals_desc,
        "gpt_sent_desc": gpt_totals_desc,
        "transfers": transfers,
        "totals_sent": cash_totals_desc,
        "gpt_sent": gpt_totals_desc,
    }


def direct_game_answer(paths: Civ6Paths, snapshot: GameSnapshot, question: str) -> str | None:
    if not looks_like_gold_spending_question(question):
        return None
    gold = gold_context(paths, snapshot)
    totals = gold.get("totals_sent_desc") or []
    gpt_totals = gold.get("gpt_sent_desc") or []
    if totals or gpt_totals:
        first = totals[0] if totals else None
        second = totals[1] if len(totals) > 1 else None
        gpt_first = gpt_totals[0] if gpt_totals else None
        cash_part = ""
        if first:
            runner = ""
            if second:
                runner = f"第二是{second.get('player_zh')}，共{format_gold_amount(second.get('total_gold_sent'))}金币。"
            cash_part = (
                f"现金转账最多的是{first.get('player_zh')}，共"
                f"{format_gold_amount(first.get('total_gold_sent'))}金币。{runner}"
            )
        gpt_part = ""
        if gpt_first:
            gpt_part = (
                f"回合金最多的是{gpt_first.get('player_zh')}，共"
                f"{format_gold_amount(gpt_first.get('total_gpt_sent'))}回合金。"
            )
        return (
            f"{cash_part}{gpt_part}"
            "买单位、买建筑这种内政消费目前不能精确拆出来。"
        )

    rows = [
        stats for stats in snapshot.stats_for_turn(snapshot.latest_turn)
        if stats.slot is not None and snapshot.identity_for(stats.slot) is not None
    ]
    gold_yield_rows = [stats for stats in rows if stats.gold_yield is not None]
    if gold_yield_rows:
        leader = max(gold_yield_rows, key=lambda stats: stats.gold_yield or 0)
        return (
            "当前没有看到玩家间金币转账。若按经济体量估计，"
            f"{player_label_zh(snapshot, leader.slot)}金币收入最高，约每回合"
            f"{format_gold_amount(leader.gold_yield)}金币，最可能有最多可支配现金。"
        )
    gold_balance_rows = [stats for stats in rows if stats.gold_balance is not None]
    if gold_balance_rows:
        leader = max(gold_balance_rows, key=lambda stats: stats.gold_balance or 0)
        return (
            "当前没有看到玩家间金币转账。若按现金储备估计，"
            f"{player_label_zh(snapshot, leader.slot)}金币余额最高，约"
            f"{format_gold_amount(leader.gold_balance)}金币。"
        )
    return "当前还没有足够经济数据判断谁花钱最多。"


def looks_like_gold_spending_question(question: str) -> bool:
    text = question.strip().lower()
    gold_words = ("金币", "金钱", "钱", "gold", "送钱", "送金", "打钱", "转账", "交易", "花钱")
    leader_words = ("谁", "哪个", "最多", "最", "第一", "leader", "most")
    return any(word in text for word in gold_words) and any(word in text for word in leader_words)


def city_states_context(paths: Civ6Paths) -> list[dict]:
    path = paths.logs_dir / "GameCore.log"
    city_states: dict[int, dict] = {}
    for line in latest_text_session_lines(path):
        match = CITY_STATE_RE.search(line)
        if not match:
            continue
        slot = int(match.group("slot"))
        civ_token = match.group("civ")
        leader_token = match.group("leader")
        name = format_game_token(civ_token.replace("CIVILIZATION_", "", 1))
        city_state_type, city_state_type_zh = CITY_STATE_TYPES.get(name, ("Unknown", "未知"))
        city_states[slot] = {
            "slot": slot,
            "name": name,
            "name_zh": zh_name(name),
            "type": city_state_type,
            "type_zh": city_state_type_zh,
            "civilization_token": civ_token,
            "leader_token": leader_token,
        }
    try:
        save_summary = read_latest_save_summary(paths, include_map=False)
    except (OSError, SaveParseError):
        save_summary = None
    if save_summary:
        for save_city_state in save_summary.get("active_city_states", []):
            slot = save_city_state.get("slot")
            name = save_city_state.get("civilization")
            if slot is None or not name or slot in city_states:
                continue
            city_state_type, city_state_type_zh = CITY_STATE_TYPES.get(name, ("Unknown", "\u672a\u77e5"))
            city_states[slot] = {
                "slot": slot,
                "name": name,
                "name_zh": zh_name(name),
                "type": city_state_type,
                "type_zh": city_state_type_zh,
                "civilization_token": save_city_state.get("civilization_token"),
                "leader_token": save_city_state.get("leader_token"),
                "source": "latest autosave header",
            }
    return [city_states[slot] for slot in sorted(city_states)]


def recent_events_context(paths: Civ6Paths, limit: int) -> list[str]:
    events = human_live_events(paths, limit=limit, important_only=True)
    text = format_events(events)
    if text == "No human live events found.":
        return []
    return text.splitlines()


def player_label(snapshot: GameSnapshot, slot: int | None) -> str:
    if slot is None:
        return "unknown"
    identity = snapshot.identity_for(slot)
    if identity is None:
        return f"player {slot}"
    civ = identity.short_civ or "unknown civ"
    if identity.player_name:
        return f"{civ} ({identity.player_name})"
    return civ


def player_label_zh(snapshot: GameSnapshot, slot: int | None) -> str:
    if slot is None:
        return "未知"
    identity = snapshot.identity_for(slot)
    if identity is None:
        return f"玩家{slot}"
    civ = zh_name(identity.short_civ)
    if identity.player_name:
        return f"{civ}（{identity.player_name}）"
    return civ or f"玩家{slot}"


def save_slot_label(save_identities: dict[int, dict], slot: int | None) -> str:
    if slot is None:
        return "unknown"
    identity = save_identities.get(slot)
    if identity is None:
        return f"player {slot}"
    return identity.get("display") or identity.get("civilization") or identity.get("name") or f"player {slot}"


def save_slot_label_zh(save_identities: dict[int, dict], slot: int | None) -> str:
    if slot is None:
        return "未知"
    identity = save_identities.get(slot)
    if identity is None:
        return f"玩家{slot}"
    return identity.get("display_zh") or identity.get("civilization_zh") or identity.get("name_zh") or f"玩家{slot}"


def zh_name(value: str | None) -> str | None:
    if value is None:
        return None
    return CITY_NAME_ZH_OVERRIDES.get(value) or ZH_NAME_MAP.get(value, value)


def city_name_zh(paths: Civ6Paths, loc_tag: str | None, display_name: str | None) -> str | None:
    if loc_tag:
        override = CITY_NAME_ZH_OVERRIDES.get(loc_tag)
        if override:
            return override
        localized = localized_text(paths.cache_dir, loc_tag)
        if localized:
            return localized
    if display_name:
        override = CITY_NAME_ZH_OVERRIDES.get(display_name)
        if override:
            return override
        guessed_tag = city_loc_tag_from_display(display_name)
        override = CITY_NAME_ZH_OVERRIDES.get(guessed_tag)
        if override:
            return override
        localized = localized_text(paths.cache_dir, guessed_tag)
        if localized:
            return localized
        return zh_name(display_name)
    return None


def city_loc_tag_from_display(display_name: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "_", display_name).strip("_").upper()
    return f"LOC_CITY_NAME_{token}"


@lru_cache(maxsize=8192)
def localized_text(cache_dir: Path, tag: str, language: str = "zh_Hans_CN") -> str | None:
    db_path = cache_dir / "DebugLocalization.sqlite"
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                "select Text from LocalizedText where Language = ? and Tag = ?",
                (language, tag),
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    return clean_localized_text(str(row[0]))


def clean_localized_text(value: str) -> str:
    # Some localized rows contain Civ's grammar variants separated by pipes.
    return value.split("|", 1)[0].strip()


def turn_label(turn: int | None) -> str | None:
    if turn is None:
        return None
    return f"{turn}T"


def format_gold_amount(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "未知"
    if math.isfinite(number) and number == int(number):
        return str(int(number))
    return f"{number:.1f}".rstrip("0").rstrip(".")


def team_label(team: int | None) -> str | None:
    if team is None:
        return None
    configured = os.environ.get(f"CIV6_TEAM_{team + 1}_NAME", "").strip()
    if configured:
        return configured
    return f"\u961f\u4f0d {team + 1}"


def parse_float(value: str) -> float | None:
    try:
        return float(value.strip())
    except (AttributeError, ValueError):
        return None
