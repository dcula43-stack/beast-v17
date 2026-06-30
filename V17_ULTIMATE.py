import os
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime

# ============================================================
#   🔥 BEAST ULTIMATE v16 — ICT + SMC + WYCKOFF
#   تعديلات v12 عن v7 (محتفظ بها):
#   1) SL ديناميكي مبني على ATR بدل extreme+buffer ثابت
#   2) RR متغير حسب قوة الـ confluence score
#   3) BE مع buffer لصالح الصفقة + تفعيل أبكر (1.5R)
#   4) Partial TP عند 1R (إغلاق 50%) + الباقي يكمل للهدف
#   5) نافذة متابعة موسّعة (حتى نهاية اليوم) + إغلاق mark-to-market
#   6) GBPUSD: شرط دخول أصعب (confluence أعلى)
#
#   تعديلات v16 الجديدة:
#   16) سقف السلّم خُفّض من 1.0% إلى 0.75% (نفس منطق السلّم، فقط مدى
#       أضيق) — لتوسيع المسافة عن حد الانفجار بعد ما كانت 2.0 نقطة فقط
#   17) فلتر الأخبار الحمراء (Red News Filter): أي صفقة يُحتمل دخولها
#       خلال ±30 دقيقة من خبر اقتصادي عالي التأثير (High Impact) تُرفض
#       تماماً — يتطلب ملف تقويم اقتصادي (CSV) يحدّده المستخدم
# ============================================================

ASSETS = [
    ("EURUSD=X", "EURUSD", "eur"),
    ("GBPUSD=X", "GBPUSD", "gbp"),
    ("GC=F",     "GOLD",   "gold"),
]

INITIAL_BALANCE   = 100000.0
RISK_PCT          = 0.01   # 🔧 [v15] رجعنا للحد الأعلى 1% — لكنه الآن سقف لسلّم متدرّج، لا قيمة ثابتة

# 🔧 [v16] سقف السلّم خُفّض من 1.0% إلى 0.75% — نفس المنطق، مدى أضيق
#     لتوسيع المسافة عن حد الانفجار (كانت 2.0 نقطة فقط في v15)
RISK_LADDER       = [0.0025, 0.005, 0.0075]
RISK_START_IDX    = 2          # نبدأ من السقف الجديد 0.75%
STEP_DOWN_AFTER   = 2          # كل 2 خسارة متتالية → درجة أدنى
STEP_UP_AFTER     = 2          # كل 2 ربح متتالي → درجة أعلى (سقفها 1%، لا تتجاوزه)
PHASE1_TARGET     = 110000.0
PHASE2_TARGET     = 105000.0
MAX_DAILY_DD_PCT  = 0.05
MAX_TOTAL_DD_PCT  = 0.10
PERIOD            = "1mo"
MAX_TRADES_DAY    = 10
MAX_CONSEC_LOSSES = 3
TREND_LOOKBACK    = 20

# 🔧 [v12] نافذة المتابعة بعد الدخول — موسّعة من 25 إلى 80 شمعة (~6.5 ساعة)
#     أي صفقة لا تصل SL/TP خلال هذه الفترة تُغلق mark-to-market (لا تُهمل)
FOLLOW_WINDOW     = 80

# 🔧 [v12] معامل ATR لحساب SL (بدل buffer ثابت)
ATR_PERIOD        = 14
ATR_SL_MULT       = 1.3

# 🔧 [v12] RR متغير حسب قوة score (إجمالي الشروط المفعّلة)
def get_rr_for_score(total_score):
    if total_score >= 7:
        return 3.0
    elif total_score >= 5:
        return 2.5
    else:
        return 2.0

# 🔧 [v12] نسبة الإغلاق الجزئي عند TP1 (1R) ونسبة الباقي للهدف الكامل
PARTIAL_TP_R      = 1.0     # TP1 عند 1×R
PARTIAL_CLOSE_PCT = 0.5     # إغلاق 50% من الحجم عند TP1

# 🔧 [v12] تفعيل BE أبكر (1.5R بدل 2R) + buffer لصالح الصفقة (0.3R)
BE_TRIGGER_R      = 1.5
BE_BUFFER_R       = 0.3

# 🔧 [v13] تكلفة Spread/Slippage الحقيقية بالـ pips لكل زوج (round-trip تقريبي)
#     هذا الرقم يُخصم من كل صفقة كـ"احتكاك" واقعي — اضبطه حسب البروكر الفعلي
SPREAD_PIPS = {
    "eur": 1.2,
    "gbp": 1.8,
    "gold": 25.0,   # بالسنت = 0.25$ لكل أونصة تقريباً، يتفاوت حسب البروكر
}

# 🔧 [v13] نسبة آخر الداتا التي تُعتبر Out-of-Sample (لم تُستخدم للضبط)
#     لفحص حقيقي للـ overfitting — لا تُضبط أي معاملات بناءً على هذه الفترة
WALK_FORWARD_OOS_PCT = 0.25   # آخر 25% من الفترة الزمنية = اختبار خارج العيّنة

# 🔧 [v14] ساعات مستثناة تماماً من التداول (أداء سلبي مؤكد بعينة كبيرة)
EXCLUDED_HOURS = {2}   # كانت -$41,925 على 214 صفقة بثقة عالية (43% WR)

# 🔧 [v16] فلتر الأخبار الحمراء — يتطلب ملف تقويم اقتصادي CSV
#     فورمات الملف المطلوب: أعمدة datetime,impact (datetime بصيغة ISO،
#     impact = "High"/"Medium"/"Low"). مصدر مقترح: تصدير من
#     ForexFactory أو Investing.com لتقويم اقتصادي مفصّل بالتاريخ.
NEWS_CSV_PATH     = "~/news_calendar.csv"
NEWS_BLACKOUT_MIN = 30   # ±30 دقيقة = نصف ساعة قبل وبعد = ساعة كاملة كما طلبت
NEWS_IMPACT_FILTER = {"High","high","HIGH"}   # فقط الأخبار الحمراء (High impact)

# ── الجلسات 4 ساعات ─────────────────────────────────────────
SESSIONS = {
    "LONDON" : (2,  6),
    "NEWYORK": (8,  12),
}

PAIR_SESSIONS = {
    "eur"  : ["LONDON", "NEWYORK", "ASIA"],
    "gbp"  : ["LONDON", "NEWYORK", "ASIA"],
    "gold" : ["NEWYORK"],
}

RANGE_LIMITS = {
    "eur": (5, 100),
    "gbp": (5, 120),
    "gold": (50, 800),
}

# 🔧 [v12] الحد الأدنى لشروط الدخول — GBP يحتاج أعلى لتحسين أداءه الضعيف
MIN_CONFLUENCE = {
    "eur"  : {"ict": 2, "smc": 1, "wyc": 1},
    "gbp"  : {"ict": 2, "smc": 2, "wyc": 1},   # GBP: شرط SMC أعلى
    "gold" : {"ict": 2, "smc": 1, "wyc": 1},
}

# ══════════════════════════════════════════════════════════════
#   ICT CONDITIONS
# ══════════════════════════════════════════════════════════════

def ict_asian_sweep(hi, lo, cl, op, ah, al, i, direction):
    """ICT 1: Asian Range Liquidity Sweep"""
    if direction == "bull":
        return lo[i] < al[i] and cl[i] > op[i]
    return hi[i] > ah[i] and cl[i] < op[i]

def ict_fvg(hi, lo, i, direction):
    """ICT 2: Fair Value Gap"""
    if i < 2: return False
    return lo[i] > hi[i-2] if direction=="bull" else hi[i] < lo[i-2]

def ict_ob(hi, lo, cl, op, i, direction):
    """ICT 3: Order Block — آخر شمعة عكسية قبل الحركة"""
    if i < 3: return False
    if direction == "bull":
        return cl[i-2] < op[i-2] and cl[i-1] > op[i-1]
    return cl[i-2] > op[i-2] and cl[i-1] < op[i-1]

def ict_cisd(hi, lo, cl, i, direction):
    """ICT 4: Change in State of Delivery"""
    if i < 5: return False
    if direction == "bull":
        return cl[i] > max(hi[i-5:i-1])
    return cl[i] < min(lo[i-5:i-1])

def ict_premium_discount(cl, ah, al, i, direction):
    """ICT 5: Premium/Discount Zone"""
    if i < 1: return False
    range_size = ah[i] - al[i]
    if range_size <= 0: return True
    equilibrium = al[i] + range_size * 0.5
    if direction == "bull":
        return cl[i] < equilibrium
    return cl[i] > equilibrium

# ══════════════════════════════════════════════════════════════
#   SMC CONDITIONS
# ══════════════════════════════════════════════════════════════

def smc_bos(hi, lo, cl, i, direction):
    """SMC 1: Break of Structure"""
    if i < 10: return False
    if direction == "bull":
        prev_high = max(hi[i-10:i-1])
        return cl[i] > prev_high
    prev_low = min(lo[i-10:i-1])
    return cl[i] < prev_low

def smc_choch(hi, lo, cl, op, i, direction):
    """SMC 2: Change of Character"""
    if i < 6: return False
    if direction == "bull":
        was_bearish = lo[i-3] < lo[i-6]
        now_bullish = cl[i] > op[i]
        return was_bearish and now_bullish
    was_bullish = hi[i-3] > hi[i-6]
    now_bearish = cl[i] < op[i]
    return was_bullish and now_bearish

def smc_liquidity_grab(hi, lo, cl, i, direction):
    """SMC 3: Liquidity Grab"""
    if i < 5: return False
    if direction == "bull":
        recent_lows = lo[i-5:i]
        min_low = min(recent_lows)
        avg_low = np.mean(recent_lows)
        grabbed = lo[i] <= min_low * 1.001
        reversed_up = cl[i] > avg_low
        return grabbed and reversed_up
    recent_highs = hi[i-5:i]
    max_high = max(recent_highs)
    avg_high = np.mean(recent_highs)
    grabbed = hi[i] >= max_high * 0.999
    reversed_down = cl[i] < avg_high
    return grabbed and reversed_down

def smc_imbalance(hi, lo, i, direction):
    """SMC 4: Imbalance"""
    if i < 2: return False
    gap = abs(lo[i] - hi[i-2]) if direction=="bull" else abs(hi[i] - lo[i-2])
    candle_size = abs(hi[i] - lo[i])
    return gap > 0 and candle_size > 0

# ══════════════════════════════════════════════════════════════
#   WYCKOFF CONDITIONS
# ══════════════════════════════════════════════════════════════

def wyckoff_spring_upthrust(hi, lo, cl, op, ah, al, i, direction):
    """Wyckoff 1: Spring / Upthrust"""
    if i < 3: return False
    if direction == "bull":
        support = min(lo[i-3:i])
        return lo[i] < support and cl[i] > support
    resistance = max(hi[i-3:i])
    return hi[i] > resistance and cl[i] < resistance

def wyckoff_test(hi, lo, cl, op, i, direction):
    """Wyckoff 2: Test of Spring/Upthrust"""
    if i < 5: return False
    current_range = hi[i] - lo[i]
    avg_range = np.mean([hi[j] - lo[j] for j in range(i-5, i)])
    small_candle = current_range < avg_range * 0.7
    if direction == "bull":
        return small_candle and cl[i] > op[i]
    return small_candle and cl[i] < op[i]

def wyckoff_cause_effect(hi, lo, cl, i, direction):
    """Wyckoff 3: Cause and Effect"""
    if i < 15: return False
    ranges = [hi[j] - lo[j] for j in range(i-15, i)]
    avg_range = np.mean(ranges)
    recent_avg = np.mean(ranges[-5:])
    compression = recent_avg < avg_range * 0.8
    breakout = hi[i] - lo[i] > avg_range * 1.2
    return compression or breakout

def wyckoff_effort_result(hi, lo, cl, op, i, direction):
    """Wyckoff 4: Effort vs Result"""
    if i < 3: return False
    candle_body = abs(cl[i] - op[i])
    candle_range = hi[i] - lo[i]
    if candle_range == 0: return False
    body_ratio = candle_body / candle_range
    if direction == "bull":
        return body_ratio > 0.5 and cl[i] > op[i]
    return body_ratio > 0.5 and cl[i] < op[i]

# ══════════════════════════════════════════════════════════════
#   CONFLUENCE SCORER
# ══════════════════════════════════════════════════════════════

def calculate_confluence(hi, lo, cl, op, ah, al, i, direction, name):
    ict_checks = {
        "ICT_SWEEP"    : ict_asian_sweep(hi,lo,cl,op,ah,al,i,direction),
        "ICT_FVG"      : ict_fvg(hi,lo,i,direction),
        "ICT_OB"       : ict_ob(hi,lo,cl,op,i,direction),
        "ICT_CISD"     : ict_cisd(hi,lo,cl,i,direction),
        "ICT_PD"       : ict_premium_discount(cl,ah,al,i,direction),
    }
    smc_checks = {
        "SMC_BOS"      : smc_bos(hi,lo,cl,i,direction),
        "SMC_CHOCH"    : smc_choch(hi,lo,cl,op,i,direction),
        "SMC_LIQ"      : smc_liquidity_grab(hi,lo,cl,i,direction),
        "SMC_IMBAL"    : smc_imbalance(hi,lo,i,direction),
    }
    wyc_checks = {
        "WYC_SPRING"   : wyckoff_spring_upthrust(hi,lo,cl,op,ah,al,i,direction),
        "WYC_TEST"     : wyckoff_test(hi,lo,cl,op,i,direction),
        "WYC_CAUSE"    : wyckoff_cause_effect(hi,lo,cl,i,direction),
        "WYC_EFFORT"   : wyckoff_effort_result(hi,lo,cl,op,i,direction),
    }

    ict_score = sum(ict_checks.values())
    smc_score = sum(smc_checks.values())
    wyc_score = sum(wyc_checks.values())
    total     = ict_score + smc_score + wyc_score

    active = {k:v for d in [ict_checks,smc_checks,wyc_checks]
              for k,v in d.items() if v}

    req = MIN_CONFLUENCE.get(name, {"ict":2,"smc":1,"wyc":1})
    qualified = (ict_score >= req["ict"] and smc_score >= req["smc"]
                 and wyc_score >= req["wyc"])

    return {
        "qualified"  : qualified,
        "total"      : total,
        "ict_score"  : ict_score,
        "smc_score"  : smc_score,
        "wyc_score"  : wyc_score,
        "active"     : active,
    }

# ══════════════════════════════════════════════════════════════
#   DATA LOADING
# ══════════════════════════════════════════════════════════════

CSV_MAP = {
    "EURUSD=X": "~/data_EURUSD_FULL_2Y.csv",
    "GBPUSD=X": "~/data_GBPUSD_FULL_2Y.csv",
    "GC=F":     "~/data_GOLD_FULL_2Y.csv",
}

def get_data_5m(ticker, name):
    print(f"  [5m] {name}...")
    csv_path = os.path.expanduser(CSV_MAP.get(ticker, ""))
    if not csv_path or not os.path.exists(csv_path):
        print(f"  [ERR] CSV not found for {ticker}")
        return pd.DataFrame()
    df = pd.read_csv(csv_path, parse_dates=["Datetime"])
    df = df.rename(columns={"Datetime":"datetime","Open":"open","High":"high",
                             "Low":"low","Close":"close","Volume":"volume"})
    df = df.set_index("datetime")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert("America/New_York")
    else:
        df.index = df.index.tz_convert("America/New_York")
    df['date_group'] = df.index.date
    df['is_asian'] = (df.index.hour >= 19) | (df.index.hour < 2)
    ah = df[df['is_asian']].groupby('date_group')['high'].max()
    al = df[df['is_asian']].groupby('date_group')['low'].min()
    df['asian_h'] = df['date_group'].map(ah).shift(24)
    df['asian_l'] = df['date_group'].map(al).shift(24)

    # 🔧 [v12] ATR(14) على 5m لاستخدامه في SL الديناميكي
    prev_close = df['close'].shift(1)
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - prev_close).abs(),
        (df['low'] - prev_close).abs()
    ], axis=1).max(axis=1)
    df['atr'] = tr.rolling(ATR_PERIOD).mean()

    print(f"  [OK] {len(df)} bars")
    return df

def get_data_1h(ticker, name):
    csv_path = os.path.expanduser(CSV_MAP.get(ticker, ""))
    if not csv_path or not os.path.exists(csv_path):
        return pd.DataFrame()
    df = pd.read_csv(csv_path, parse_dates=["Datetime"])
    df = df.rename(columns={"Datetime":"datetime","Open":"open","High":"high",
                             "Low":"low","Close":"close","Volume":"volume"})
    df = df.set_index("datetime")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert("America/New_York")
    else:
        df.index = df.index.tz_convert("America/New_York")
    df = df.resample("1h").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()
    df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
    return df

def get_htf_bias(htf_df, t):
    try:
        past = htf_df[htf_df.index <= t]
        if len(past) < 3: return None
        last = past.iloc[-1]
        if np.isnan(last['ema20']): return None
        return "bull" if last['close'] > last['ema20'] else "bear"
    except: return None

def get_session(hour):
    for sname,(s,e) in SESSIONS.items():
        if s<=hour<e: return sname
    return None

def check_range(ah, al, i, name):
    if i<1: return False
    r=ah[i]-al[i]
    if r<=0: return False
    pip = 0.1 if name=='gold' else 0.0001
    pips=r/pip
    lo_lim, hi_lim = RANGE_LIMITS.get(name, (5,250))
    return lo_lim<=pips<=hi_lim

def get_spread_cost_r(name, r_distance):
    """🔧 [v13] يحوّل spread (pips) إلى نسبة من R لخصمها من كل صفقة"""
    pip = 0.1 if name == 'gold' else 0.0001
    spread_price = SPREAD_PIPS.get(name, 1.5) * pip
    if r_distance <= 0: return 0.0
    return spread_price / r_distance

def load_news_calendar():
    """🔧 [v16] يحمّل تقويم الأخبار ويبني مجموعة timestamps محظورة"""
    path = os.path.expanduser(NEWS_CSV_PATH)
    if not os.path.exists(path):
        print(f"  [WARN] ملف التقويم الاقتصادي غير موجود: {path}")
        print(f"  [WARN] فلتر الأخبار الحمراء معطّل لهذا التشغيل (لا حظر)")
        return []
    try:
        ndf = pd.read_csv(path, parse_dates=["datetime"])
        ndf = ndf[ndf["impact"].isin(NEWS_IMPACT_FILTER)]
        if ndf["datetime"].dt.tz is None:
            ndf["datetime"] = ndf["datetime"].dt.tz_localize("UTC").dt.tz_convert("America/New_York")
        else:
            ndf["datetime"] = ndf["datetime"].dt.tz_convert("America/New_York")
        print(f"  [OK] تحميل {len(ndf)} خبر عالي التأثير من التقويم")
        return sorted(ndf["datetime"].tolist())
    except Exception as e:
        print(f"  [ERR] فشل قراءة ملف التقويم: {e}")
        return []

import bisect

def is_news_blackout(t, news_times, window_min=NEWS_BLACKOUT_MIN):
    """🔧 [v16] True لو الوقت t يقع ضمن ±window_min من خبر أحمر (بحث ثنائي سريع)"""
    if not news_times: return False
    window = pd.Timedelta(minutes=window_min)
    pos = bisect.bisect_left(news_times, t)
    # نفحص أقرب خبر قبل وبعد فقط (القائمة مرتبة)
    for idx in (pos-1, pos):
        if 0 <= idx < len(news_times):
            if abs((t - news_times[idx]).total_seconds()) <= window.total_seconds():
                return True
    return False

def get_loss_reason(hi,lo,cl,op,ah,al,i,direction,be_on):
    reasons=[]
    if be_on: reasons.append("BE_REVERSAL")
    if direction=="bull":
        if lo[i]<al[i-1]: reasons.append("ASIAN_LOW_BROKEN")
        if cl[i]<op[i]: reasons.append("BEARISH_CANDLE")
    else:
        if hi[i]>ah[i-1]: reasons.append("ASIAN_HIGH_BROKEN")
        if cl[i]>op[i]: reasons.append("BULLISH_CANDLE")
    if abs(hi[i]-lo[i])>abs(hi[i-1]-lo[i-1])*2: reasons.append("BIG_CANDLE")
    return reasons if reasons else ["NORMAL_SL"]

# ══════════════════════════════════════════════════════════════
#   MAIN
# ══════════════════════════════════════════════════════════════

print("="*64)
print("  🔥 BEAST ULTIMATE v16 — ICT + SMC + WYCKOFF")
print(f"  SL: ATR×{ATR_SL_MULT} | RR: متغير (2.0-3.0 حسب score)")
print(f"  BE: عند {BE_TRIGGER_R}R + buffer {BE_BUFFER_R}R | TP1: {PARTIAL_TP_R}R ({int(PARTIAL_CLOSE_PCT*100)}%)")
print(f"  نافذة المتابعة: {FOLLOW_WINDOW} شمعة | Max {MAX_TRADES_DAY}/day | Loss Limit: {MAX_CONSEC_LOSSES}")
print(f"  [v13] Spread cost حقيقي مُفعّل | OOS check آخر {int(WALK_FORWARD_OOS_PCT*100)}% من الفترة")
print("="*64)

frames_5m={}; frames_1h={}
for ticker,name,suffix in ASSETS:
    df5=get_data_5m(ticker,name)
    if not df5.empty: frames_5m[suffix]=df5
    df1=get_data_1h(ticker,name)
    if not df1.empty: frames_1h[suffix]=df1

if len(frames_5m)<2: print("[-] Not enough data!"); exit()

base=list(frames_5m.keys())[0]
combined=frames_5m[base].add_suffix(f"_{base}")
combined.index.name=None
for name in list(frames_5m.keys())[1:]:
    suf=frames_5m[name].add_suffix(f"_{name}")
    combined=pd.merge_asof(combined.sort_index(),suf.sort_index(),
                           left_index=True,right_index=True,direction='backward')

print(f"[+] Bars : {len(combined)}")
print(f"[+] Range: {combined.index[0].date()} -> {combined.index[-1].date()}")
print("[+] Running...\n")

# 🔧 [v16] تحميل تقويم الأخبار الحمراء (إن وُجد ملف)
print("[+] تحميل تقويم الأخبار...")
NEWS_TIMES = load_news_calendar()

balance=INITIAL_BALANCE; phase=1
p1_pass_date=p2_pass_date=None
p1_trades=p2_trades=funded_trades=0
funded_profit=0.0; start_date=combined.index[0]
current_day=None; daily_start_bal=INITIAL_BALANCE
daily_loss=0.0; daily_trade_count=0; consec_losses=0
# 🔧 [v15] سلّم المخاطرة المتدرّج — مستقل عن daily reset (يستمر عبر الأيام،
#     فقط نتيجة الصفقات الفعلية تحرّكه، لا تغيّر اليوم)
risk_idx=RISK_START_IDX; ladder_loss_streak=0; ladder_win_streak=0
risk_level_log=[]   # لتتبّع تغيّرات مستوى المخاطرة عبر الزمن
account_blown=False; all_pnls=[]; trades_log=[]
equity_curve=[INITIAL_BALANCE]
# 🔧 [v13] true_equity تراكمي مستقل عن phase resets — لحساب drawdown صحيح
true_cum_pnl=0.0; true_equity_curve=[INITIAL_BALANCE]; true_peak=INITIAL_BALANCE
daily_results={}; monthly_results={}; hour_results={}

session_stats={s:{"trades":0,"wins":0,"losses":0,"be":0,"profit":0.0}
               for s in SESSIONS}
pair_stats={n:{"trades":0,"wins":0,"losses":0,"be":0,"profit":0.0,
               "session_profit":{s:0.0 for s in SESSIONS},
               "loss_reasons":{}}
            for n in frames_5m.keys()}
direction_stats={
    "bull":{"trades":0,"wins":0,"losses":0,"be":0,"profit":0.0},
    "bear":{"trades":0,"wins":0,"losses":0,"be":0,"profit":0.0},
}
school_stats={
    "ICT" :{"triggered":0,"wins":0,"total_score":0},
    "SMC" :{"triggered":0,"wins":0,"total_score":0},
    "WYC" :{"triggered":0,"wins":0,"total_score":0},
}
condition_stats={}
filter_saves={
    "htf":0,"range":0,"session":0,
    "maxday":0,"consec":0,"confluence":0,
    "fakeout_dist":0,"candle_confirm":0,"big_candle":0,"no_atr":0,
    "excluded_hour":0,"news_blackout":0,
}
exit_type_stats={"TP_FULL":0,"TP_PARTIAL_BE":0,"TP_PARTIAL_SL":0,
                  "BE_FLAT":0,"SL_FULL":0,"TIMEOUT_MTM":0}

for i in range(25,len(combined)-FOLLOW_WINDOW-1):
    if account_blown: break
    t=combined.index[i]

    if current_day!=t.date():
        current_day=t.date(); daily_start_bal=balance
        daily_loss=0.0; daily_trade_count=0; consec_losses=0

    if daily_loss>=daily_start_bal*MAX_DAILY_DD_PCT: continue
    # 🔧 [v14] فحص الانفجار الآن يعتمد على true_equity الحقيقي المستقل
    #     عن phase resets — لا balance الذي يُصفَّر عند كل نجاح phase
    current_true_eq = INITIAL_BALANCE + true_cum_pnl
    if (true_peak-current_true_eq)>=INITIAL_BALANCE*MAX_TOTAL_DD_PCT:
        account_blown=True; break
    if daily_trade_count>=MAX_TRADES_DAY:
        filter_saves["maxday"]+=1; continue
    if consec_losses>=MAX_CONSEC_LOSSES:
        filter_saves["consec"]+=1; continue

    session=get_session(t.hour)
    if not session: continue
    if t.hour in EXCLUDED_HOURS:
        filter_saves["excluded_hour"]+=1; continue

    # 🔧 [v16] فحص الأخبار الحمراء — ±30 دقيقة (نصف ساعة قبل وبعد = ساعة كاملة)
    if is_news_blackout(t, NEWS_TIMES):
        filter_saves["news_blackout"]+=1; continue

    for name in frames_5m.keys():
        allowed=PAIR_SESSIONS.get(name,list(SESSIONS.keys()))
        if session not in allowed:
            filter_saves["session"]+=1; continue

        try:
            hi=combined[f'high_{name}'].values
            lo=combined[f'low_{name}'].values
            cl=combined[f'close_{name}'].values
            op=combined[f'open_{name}'].values
            ah=combined[f'asian_h_{name}'].values
            al=combined[f'asian_l_{name}'].values
            atr=combined[f'atr_{name}'].values
        except KeyError: continue

        if np.isnan(ah[i-1]) or np.isnan(al[i-1]): continue
        if np.isnan(atr[i-1]) or atr[i-1]<=0:
            filter_saves["no_atr"]+=1; continue

        if not check_range(ah,al,i-1,name):
            filter_saves["range"]+=1; continue

        direction=None
        if lo[i-1]<al[i-1] and cl[i-1]>op[i-1]: direction="bull"
        elif hi[i-1]>ah[i-1] and cl[i-1]<op[i-1]: direction="bear"
        if not direction: continue

        rng = ah[i-1] - al[i-1]
        if direction == "bull":
            dist_to_other_side = ah[i-1] - cl[i-1]
        else:
            dist_to_other_side = cl[i-1] - al[i-1]
        if rng > 0 and dist_to_other_side < rng * 0.15:
            filter_saves["fakeout_dist"]+=1; continue

        if direction == "bull" and cl[i-1] <= op[i-1]:
            filter_saves["candle_confirm"]+=1; continue
        if direction == "bear" and cl[i-1] >= op[i-1]:
            filter_saves["candle_confirm"]+=1; continue

        candle_size = hi[i-1]-lo[i-1]
        avg_candle = np.mean(hi[i-11:i-1]-lo[i-11:i-1])
        if avg_candle>0 and candle_size > avg_candle*2.0:
            filter_saves["big_candle"]+=1; continue

        if name in frames_1h:
            bias=get_htf_bias(frames_1h[name],t)
            if bias and bias!=direction:
                filter_saves["htf"]+=1; continue

        conf=calculate_confluence(hi,lo,cl,op,ah,al,i-1,direction,name)
        if not conf["qualified"]:
            filter_saves["confluence"]+=1; continue

        for cname in conf["active"]:
            if cname not in condition_stats:
                condition_stats[cname]={"count":0,"wins":0}
            condition_stats[cname]["count"]+=1

        current_risk_pct = RISK_LADDER[risk_idx]
        risk_amount=balance*current_risk_pct
        rr_ratio = get_rr_for_score(conf["total"])

        # 🔧 [v12] SL = ATR × معامل بدل extreme + buffer ثابت
        entry=(hi[i-1]+lo[i-1])/2
        sl_dist = atr[i-1]*ATR_SL_MULT
        if direction=="bull":
            sl=entry-sl_dist; r=sl_dist
            tp=entry+r*rr_ratio
            tp1=entry+r*PARTIAL_TP_R
            be_trigger=entry+r*BE_TRIGGER_R
            be_level=entry+r*BE_BUFFER_R
        else:
            sl=entry+sl_dist; r=sl_dist
            tp=entry-r*rr_ratio
            tp1=entry-r*PARTIAL_TP_R
            be_trigger=entry-r*BE_TRIGGER_R
            be_level=entry-r*BE_BUFFER_R
        if r<=0: continue

        # 🔧 [v13] تكلفة spread/slippage الحقيقية كنسبة من R — تُخصم من كل صفقة
        spread_cost_r = get_spread_cost_r(name, r)
        spread_cost_dollar = risk_amount*spread_cost_r

        fh=combined[f'high_{name}'].values[i:i+FOLLOW_WINDOW]
        fl=combined[f'low_{name}'].values[i:i+FOLLOW_WINDOW]
        fc=combined[f'close_{name}'].values[i:i+FOLLOW_WINDOW]

        pnl=None; exit_type=None
        triggered=False; be_on=False; partial_done=False; cur_sl=sl
        remaining_pct=1.0; realized=0.0
        last_close=None

        for h,l,c in zip(fh,fl,fc):
            last_close=c
            if not triggered and l<=entry<=h: triggered=True; continue
            if not triggered: continue

            if direction=="bull":
                if not be_on and h>=be_trigger:
                    cur_sl=be_level; be_on=True
                # 🔧 [v13] فحص SL/BE أولاً (متشائم) قبل partial TP في نفس الشمعة
                if l<=cur_sl:
                    if be_on:
                        realized += risk_amount*BE_BUFFER_R*remaining_pct
                        exit_type="TP_PARTIAL_BE" if partial_done else "BE_FLAT"
                    else:
                        realized += -risk_amount*remaining_pct
                        exit_type="TP_PARTIAL_SL" if partial_done else "SL_FULL"
                    pnl=realized; break
                if not partial_done and h>=tp1:
                    realized += risk_amount*PARTIAL_TP_R*PARTIAL_CLOSE_PCT
                    remaining_pct -= PARTIAL_CLOSE_PCT
                    partial_done=True
                if h>=tp:
                    realized += risk_amount*rr_ratio*remaining_pct
                    exit_type="TP_FULL"
                    pnl=realized; break
            else:
                if not be_on and l<=be_trigger:
                    cur_sl=be_level; be_on=True
                # 🔧 [v13] فحص SL/BE أولاً (متشائم) قبل partial TP في نفس الشمعة
                if h>=cur_sl:
                    if be_on:
                        realized += risk_amount*BE_BUFFER_R*remaining_pct
                        exit_type="TP_PARTIAL_BE" if partial_done else "BE_FLAT"
                    else:
                        realized += -risk_amount*remaining_pct
                        exit_type="TP_PARTIAL_SL" if partial_done else "SL_FULL"
                    pnl=realized; break
                if not partial_done and l<=tp1:
                    realized += risk_amount*PARTIAL_TP_R*PARTIAL_CLOSE_PCT
                    remaining_pct -= PARTIAL_CLOSE_PCT
                    partial_done=True
                if l<=tp:
                    realized += risk_amount*rr_ratio*remaining_pct
                    exit_type="TP_FULL"
                    pnl=realized; break

        # 🔧 [v13] خصم تكلفة spread/slippage الحقيقية من كل صفقة مكتملة
        if pnl is not None:
            pnl -= spread_cost_dollar

        # 🔧 [v12] لا نتجاهل الصفقة إن لم تكتمل — نغلقها mark-to-market
        if pnl is None:
            if not triggered or last_close is None:
                continue
            if direction=="bull":
                mtm_r = (last_close-entry)/r
            else:
                mtm_r = (entry-last_close)/r
            pnl = realized + risk_amount*mtm_r*remaining_pct
            pnl -= spread_cost_dollar
            exit_type="TIMEOUT_MTM"

        be_on_final = exit_type in ("TP_PARTIAL_BE","BE_FLAT")
        if pnl<0: daily_loss+=abs(pnl); consec_losses+=1
        else: consec_losses=0
        daily_trade_count+=1; all_pnls.append(pnl)

        # 🔧 [v15] تحديث سلّم المخاطرة المتدرّج حسب نتيجة الصفقة الفعلية
        old_risk_idx = risk_idx
        if pnl < 0:
            ladder_win_streak = 0
            ladder_loss_streak += 1
            if ladder_loss_streak >= STEP_DOWN_AFTER:
                risk_idx = max(0, risk_idx-1)
                ladder_loss_streak = 0
        elif pnl > 0:
            ladder_loss_streak = 0
            ladder_win_streak += 1
            if ladder_win_streak >= STEP_UP_AFTER:
                risk_idx = min(len(RISK_LADDER)-1, risk_idx+1)
                ladder_win_streak = 0
        # BE (pnl==0) لا يُغيّر أي streak — صفقة محايدة
        if risk_idx != old_risk_idx:
            risk_level_log.append({
                "date":t.strftime("%Y-%m-%d %H:%M"),
                "from":RISK_LADDER[old_risk_idx]*100,
                "to":RISK_LADDER[risk_idx]*100,
            })

        result="WIN" if pnl>0 else("BE" if abs(pnl)<1e-6 else "LOSS")
        loss_reasons=get_loss_reason(hi,lo,cl,op,ah,al,i,direction,be_on_final) if pnl<0 else []
        exit_type_stats[exit_type]=exit_type_stats.get(exit_type,0)+1

        for cname in conf["active"]:
            if pnl>0: condition_stats[cname]["wins"]+=1

        for school,key in [("ICT","ict_score"),("SMC","smc_score"),("WYC","wyc_score")]:
            school_stats[school]["triggered"]+=1
            school_stats[school]["total_score"]+=conf[key]
            if pnl>0: school_stats[school]["wins"]+=1

        hk=t.hour
        if hk not in hour_results: hour_results[hk]=[]
        hour_results[hk].append(pnl)

        ss=session_stats[session]; ss["trades"]+=1; ss["profit"]+=pnl
        if pnl>0: ss["wins"]+=1
        elif pnl<0: ss["losses"]+=1
        else: ss["be"]+=1

        ps=pair_stats[name]; ps["trades"]+=1; ps["profit"]+=pnl
        if session in ps["session_profit"]: ps["session_profit"][session]+=pnl
        if pnl>0: ps["wins"]+=1
        elif pnl<0:
            ps["losses"]+=1
            for r_ in loss_reasons: ps["loss_reasons"][r_]=ps["loss_reasons"].get(r_,0)+1
        else: ps["be"]+=1

        ds=direction_stats[direction]; ds["trades"]+=1; ds["profit"]+=pnl
        if pnl>0: ds["wins"]+=1
        elif pnl<0: ds["losses"]+=1
        else: ds["be"]+=1

        if phase==1:
            balance+=pnl; p1_trades+=1
            if balance>=PHASE1_TARGET: phase=2;p1_pass_date=t;balance=INITIAL_BALANCE
        elif phase==2:
            balance+=pnl; p2_trades+=1
            if balance>=PHASE2_TARGET: phase=3;p2_pass_date=t;balance=INITIAL_BALANCE
        elif phase==3:
            funded_profit+=pnl; funded_trades+=1

        eq=balance if phase<3 else INITIAL_BALANCE+funded_profit
        equity_curve.append(eq)

        # 🔧 [v13] true_equity تراكمي حقيقي — لا يُعاد ضبطه عند نجاح phase
        #     هذا يحل bug Max Drawdown=$0.00 لأنه يحسب الهبوط الفعلي من القمة
        true_cum_pnl += pnl
        true_eq = INITIAL_BALANCE + true_cum_pnl
        true_equity_curve.append(true_eq)
        true_peak = max(true_peak, true_eq)

        dk=t.strftime("%Y-%m-%d"); mk=t.strftime("%Y-%m")
        if dk not in daily_results: daily_results[dk]=[]
        daily_results[dk].append(pnl)
        if mk not in monthly_results: monthly_results[mk]=[]
        monthly_results[mk].append(pnl)

        trades_log.append({
            "date":t.strftime("%Y-%m-%d"),"time":t.strftime("%H:%M"),
            "asset":name.upper(),"dir":direction.upper(),
            "session":session,"pnl":round(pnl,2),
            "result":result,"phase":phase,"be":be_on_final,
            "loss_reasons":loss_reasons,"exit_type":exit_type,
            "rr":rr_ratio,"risk_pct":round(current_risk_pct*100,2),
            "score":conf["total"],
            "ict":conf["ict_score"],"smc":conf["smc_score"],"wyc":conf["wyc_score"],
            "conditions":list(conf["active"].keys()),
        })
        break

# ══════════════════════════════════════════════════════════════
#   DASHBOARD
# ══════════════════════════════════════════════════════════════
wins=[p for p in all_pnls if p>0]
losses=[p for p in all_pnls if p<0]
bes=[p for p in all_pnls if abs(p)<1e-6]
dec=len(wins)+len(losses)
wr=len(wins)/dec*100 if dec>0 else 0
pf=sum(wins)/abs(sum(losses)) if losses else 0
avg_w=sum(wins)/len(wins) if wins else 0
avg_l=sum(losses)/len(losses) if losses else 0
exp=(wr/100*avg_w)+((1-wr/100)*avg_l)
max_dd=max([true_peak-e for e in true_equity_curve]) if true_equity_curve else 0
# 🔧 [v13] drawdown الصحيح يُحسب الآن من true_equity_curve (مستقل عن phase resets)
# لمزيد من الدقة: drawdown من القمة المتحركة لحظة بلحظة (running peak) لا من قمة واحدة ثابتة
running_peak=INITIAL_BALANCE; max_dd_running=0.0
for e in true_equity_curve:
    running_peak=max(running_peak,e)
    max_dd_running=max(max_dd_running, running_peak-e)
max_dd=max_dd_running
max_dd_pct=max_dd/INITIAL_BALANCE*100

cur_w=cur_l=max_ws=max_ls=0
for p in all_pnls:
    if p>0: cur_w+=1;cur_l=0;max_ws=max(max_ws,cur_w)
    elif p<0: cur_l+=1;cur_w=0;max_ls=max(max_ls,cur_l)
    else: cur_w=cur_l=0

days_p1=(p1_pass_date-start_date).days if p1_pass_date else None
days_p2=(p2_pass_date-p1_pass_date).days if(p2_pass_date and p1_pass_date)else None
total_d=(p2_pass_date-start_date).days if p2_pass_date else None
payout=funded_profit*0.80 if funded_profit>0 else 0
win_days=sum(1 for v in daily_results.values() if sum(v)>0)
lose_days=sum(1 for v in daily_results.values() if sum(v)<0)
n_days=max(len(daily_results),1)
trades_per_day=len(all_pnls)/n_days
S="="*64; s="-"*64

print(f"\n{S}")
print(f"  🏆 BEAST ULTIMATE v16 — COMPLETE DASHBOARD")
print(f"  {combined.index[0].date()} -> {combined.index[-1].date()}")
print(S)

print(f"{'PROP FIRM':^64}")
print(s)
p1s='PASSED ✅' if p1_pass_date else 'FAILED ❌'
p2s='PASSED ✅' if p2_pass_date else 'FAILED ❌'
print(f"Phase 1 (+10%) : {p1s} | {p1_trades}T | {days_p1} days")
print(f"Phase 2 (+5%)  : {p2s} | {p2_trades}T | {days_p2} days")
print(f"Eval Time      : {total_d} days")
print(f"Blown          : {'YES 💥' if account_blown else 'NO ✅'}")

print(f"\n{'TRADE STATS':^64}")
print(s)
print(f"Total Trades   : {len(all_pnls)}")
print(f"Trades/Day Avg : {trades_per_day:.2f}  (هدفك: 3-6)")
print(f"Wins           : {len(wins)} ({wr:.1f}%)")
print(f"Losses         : {len(losses)}")
print(f"Breakevens     : {len(bes)}")
print(f"Win Rate       : {wr:.2f}%")
print(f"Profit Factor  : {pf:.2f}")
print(f"Expectancy     : ${exp:,.2f}")
print(f"Avg Win        : ${avg_w:,.2f}")
print(f"Avg Loss       : ${avg_l:,.2f}")
print(f"Max Drawdown   : ${max_dd:,.2f} ({max_dd_pct:.2f}%)")
print(f"Max Win Streak : {max_ws} | Max Loss: {max_ls}")
print(f"Win Days       : {win_days} | Loss Days: {lose_days}")
print(f"Funded Trades  : {funded_trades}")
print(f"Net Payout 80% : ${payout:,.2f}")

# 💬 [v14] شرح تلقائي لقسم TRADE STATS
dd_risk_distance = MAX_TOTAL_DD_PCT*100 - max_dd_pct
print(f"💬 شرح: Win Rate {wr:.1f}% مع PF {pf:.2f} يعني كل $1 خسارة يقابله ${pf:.2f} ربح.")
print(f"   Max Drawdown {max_dd_pct:.1f}% — المسافة المتبقية عن حد الانفجار "
      f"({MAX_TOTAL_DD_PCT*100:.0f}%) هي {dd_risk_distance:.1f} نقطة فقط.")
if dd_risk_distance < 3:
    print(f"   ⚠️ المسافة ضيقة جداً — خطر انفجار حقيقي مرتفع بهذا الحجم من الصفقة.")

# 🔧 [v15] خانة جديدة: تحليل سلّم المخاطرة المتدرّج
print(f"\n{'RISK LADDER ANALYSIS (v16)':^64}")
print(s)
ladder_levels_pct = sorted([round(r*100,2) for r in RISK_LADDER], reverse=True)
time_at_level={lvl:0 for lvl in ladder_levels_pct}
for tr in trades_log:
    time_at_level[tr["risk_pct"]] = time_at_level.get(tr["risk_pct"],0)+1
total_tr=max(len(trades_log),1)
for lvl in ladder_levels_pct:
    cnt=time_at_level.get(lvl,0)
    pct=cnt/total_tr*100
    bar="█"*int(pct/5)
    print(f"  {lvl:>4}% {bar:<20} {cnt:>5} صفقة ({pct:.1f}%)")
print(f"  عدد مرات تغيّر المستوى: {len(risk_level_log)} مرة")
downgrades=sum(1 for r in risk_level_log if r["to"]<r["from"])
upgrades=sum(1 for r in risk_level_log if r["to"]>r["from"])
print(f"    تنازل (بعد خسارتين): {downgrades} | ترقية (بعد ربحين): {upgrades}")
print(f"💬 شرح: السلّم يبدأ من 1% ويتحرك تلقائياً — نزول بعد كل 2 خسارة\n"
      f"   متتالية (حد أدنى 0.25%)، وصعود بعد كل 2 ربح متتالي (حد أعلى 1%).\n"
      f"   الهدف: تقليل الخطر تلقائياً في فترات الأداء الضعيف (تخفيف الألم)،\n"
      f"   والاستفادة الكاملة (1%) فقط في فترات الأداء القوي المؤكدة بصفقتين\n"
      f"   رابحتين متتاليتين. لو أغلب الوقت عند 1% فهذا يعني سلاسل خسارة\n"
      f"   نادرة (صحي)، ولو أغلب الوقت عند 0.25% فهذا يعني تذبذب كثير\n"
      f"   (يستحق مراجعة جودة الدخول لا فقط حجم المخاطرة).")

# 🔧 [v13] Walk-Forward Out-of-Sample check — أداء آخر فترة لم تُستخدم للضبط
print(f"\n{'WALK-FORWARD: IN-SAMPLE vs OUT-OF-SAMPLE (v13 جديد)':^64}")
print(s)
all_dates=[datetime.strptime(tr["date"],"%Y-%m-%d") for tr in trades_log]
if all_dates:
    d0,d1=min(all_dates),max(all_dates)
    total_span=(d1-d0).days
    split_date=d0 + pd.Timedelta(days=int(total_span*(1-WALK_FORWARD_OOS_PCT)))
    is_trades=[tr for tr in trades_log if datetime.strptime(tr["date"],"%Y-%m-%d")<split_date]
    oos_trades=[tr for tr in trades_log if datetime.strptime(tr["date"],"%Y-%m-%d")>=split_date]

    def quick_stats(trs):
        pnls=[tr["pnl"] for tr in trs]
        w=[p for p in pnls if p>0]; l=[p for p in pnls if p<0]
        dec_=len(w)+len(l)
        wr_=len(w)/dec_*100 if dec_>0 else 0
        pf_=sum(w)/abs(sum(l)) if l else 0
        exp_=sum(pnls)/len(pnls) if pnls else 0
        return len(trs),wr_,pf_,exp_,sum(pnls)

    n_is,wr_is,pf_is,exp_is,tot_is=quick_stats(is_trades)
    n_oos,wr_oos,pf_oos,exp_oos,tot_oos=quick_stats(oos_trades)
    print(f"  IN-SAMPLE  ({d0.date()} -> {split_date.date()})")
    print(f"    {n_is}T | WR {wr_is:.1f}% | PF {pf_is:.2f} | Exp ${exp_is:,.0f} | Total ${tot_is:,.0f}")
    print(f"  OUT-OF-SAMPLE ({split_date.date()} -> {d1.date()})")
    print(f"    {n_oos}T | WR {wr_oos:.1f}% | PF {pf_oos:.2f} | Exp ${exp_oos:,.0f} | Total ${tot_oos:,.0f}")
    if pf_oos < pf_is*0.6 or wr_oos < wr_is-15:
        print(f"  ⚠️  تحذير: تراجع واضح في OOS — احتمال overfitting، راجع المعاملات")
    else:
        print(f"  ✅ الأداء في OOS متماسك نسبياً مع IN-SAMPLE")
    print(f"💬 شرح: IN-SAMPLE هي الفترة التي تشبه ما ضبطنا عليه المعاملات،\n"
          f"   OOS هي 'اختبار حقيقي' لم تتأثر بأي تعديل. تقارب الأرقام بينهما\n"
          f"   (لا انهيار حاد في OOS) هو أقوى دليل عملي على أن النظام يلتقط\n"
          f"   نمطاً حقيقياً في السوق، وليس مجرد حفظ لتفاصيل بيانات الماضي.")
else:
    print("  لا توجد صفقات كافية للتحليل")

print(f"\n{'EXIT TYPE BREAKDOWN (v12 جديد)':^64}")
print(s)
for et,cnt in exit_type_stats.items():
    if cnt==0: continue
    pct=cnt/max(len(all_pnls),1)*100
    print(f"  {et:<16}: {cnt:>5} ({pct:.1f}%)")
sl_pct = exit_type_stats.get("SL_FULL",0)/max(len(all_pnls),1)*100
tp_pct = exit_type_stats.get("TP_FULL",0)/max(len(all_pnls),1)*100
print(f"💬 شرح: TP_FULL يعني وصل للهدف الكامل، SL_FULL يعني ضرب الستوب\n"
      f"   كاملاً (لا حماية BE تفعّلت). TP_PARTIAL_BE/SL تعني تحرّك السعر\n"
      f"   أولاً لصالحنا (TP1) ثم رجع، فأقفلنا جزء أو خسرنا الباقي عند BE/SL.\n"
      f"   نسبة SL_FULL ({sl_pct:.1f}%) مقابل TP_FULL ({tp_pct:.1f}%) تعكس\n"
      f"   فعلياً قوة منطق الدخول بعد خصم تكلفة السبريد.")

print(f"\n{'SCHOOL PERFORMANCE':^64}")
print(s)
for school,ss in school_stats.items():
    if ss["triggered"]==0: continue
    swr=round(ss["wins"]/ss["triggered"]*100,1)
    avg_sc=round(ss["total_score"]/ss["triggered"],1)
    print(f"  {school}: {ss['triggered']}T | {ss['wins']}W | "
          f"{swr}% WR | Avg Score: {avg_sc}")
print(f"💬 شرح: الثلاث مدارس تُحسب على كل الصفقات نفسها (نفس WR) لأن كل\n"
      f"   صفقة تُصنّف بثلاثة scores معاً (ICT+SMC+WYC) لا بمدرسة منفردة.\n"
      f"   'Avg Score' يقول كم شرط من كل مدرسة كان نشطاً بالمعدل — رقم\n"
      f"   أعلى يعني تلك المدرسة 'تساهم' أكثر في قرار الدخول.")

print(f"\n{'TOP CONDITIONS':^64}")
print(s)
for cname,cs in sorted(condition_stats.items(),
                        key=lambda x:-x[1]["wins"],reverse=False)[::-1][:10]:
    cwr=round(cs["wins"]/cs["count"]*100,1) if cs["count"]>0 else 0
    school="ICT" if "ICT" in cname else("SMC" if "SMC" in cname else "WYC")
    print(f"  [{school}] {cname:<18}: {cs['count']}T | "
          f"{cs['wins']}W | {cwr}% WR")
print(f"💬 شرح: هذا WR لكل شرط منفرد (لا شرط الدخول الكامل). شرط بنسبة\n"
      f"   عالية (60%+) يستحق وزن أكبر في فلتر الدخول مستقبلاً، وشرط\n"
      f"   ضعيف (<50%) قد يحتاج مراجعة أو حتى استبعاد من المعادلة.")

print(f"\n{'SESSION ANALYSIS':^64}")
print(s)
for sname,ss in session_stats.items():
    if ss["trades"]==0: continue
    sdec=ss["wins"]+ss["losses"]
    swr=round(ss["wins"]/sdec*100,1) if sdec>0 else 0
    tag="✅" if ss["profit"]>0 else "❌"
    bar="█"*int(swr/10)
    print(f"  {sname:<8} {bar:<10} {swr:>5}% | "
          f"{ss['trades']}T {ss['wins']}W {ss['losses']}L | "
          f"${ss['profit']:,.0f} {tag}")
best_session = max(session_stats.items(), key=lambda x: x[1]["profit"])[0] if session_stats else None
print(f"💬 شرح: الفرق بين الجلستين يعكس فرق السيولة وتقلب السوق. جلسة\n"
      f"   {best_session} هي الأقوى ربحاً هنا — قد يستحق التفكير في تخصيص\n"
      f"   نسبة أعلى من رأس المال أو خطر أكبر قليلاً لها مستقبلاً.")

print(f"\n{'DIRECTION':^64}")
print(s)
for d,ds in direction_stats.items():
    if ds["trades"]==0: continue
    ddec=ds["wins"]+ds["losses"]
    dwr=round(ds["wins"]/ddec*100,1) if ddec>0 else 0
    tag="✅" if ds["profit"]>0 else "❌"
    print(f"  {d.upper():<5}: {ds['trades']}T | {ds['wins']}W "
          f"{ds['losses']}L | {dwr}% WR | ${ds['profit']:,.0f} {tag}")
print(f"💬 شرح: تقارب BULL/BEAR (لا انحياز قوي لاتجاه واحد) يعني النظام\n"
      f"   لا يعتمد على ترند سوق معيّن (صاعد فقط أو نازل فقط) — هذا جيد\n"
      f"   لأنه يقلل الاعتماد على ظرف سوقي واحد قد لا يتكرر.")

print(f"\n{'HOUR ANALYSIS':^64}")
print(s)
for h in sorted(hour_results.keys()):
    hp=hour_results[h]
    hw=sum(1 for p in hp if p>0); hl=sum(1 for p in hp if p<0)
    hwr=round(hw/(hw+hl)*100,1) if (hw+hl)>0 else 0
    htot=sum(hp); tag="✅" if htot>0 else "❌"
    bar="█"*int(hwr/10)
    print(f"  {h:02d}:00 {bar:<10} {hwr:>5}% | "
          f"{len(hp)}T {hw}W {hl}L | ${htot:,.0f} {tag}")
worst_hours=[h for h,hp in hour_results.items() if sum(hp)<0]
print(f"💬 شرح: أي ساعة بعلامة ❌ هنا (خصوصاً بعينة 100+ صفقة) هي مرشّح\n"
      f"   قوي للإضافة إلى EXCLUDED_HOURS في v15 — تماماً كما حذفنا 02:00\n"
      f"   هذه المرة. ساعات حالية سلبية: {worst_hours if worst_hours else 'لا توجد'}.")

print(f"\n{'PER-PAIR':^64}")
print(s)
for pname,ps in pair_stats.items():
    if ps["trades"]==0: continue
    pdec=ps["wins"]+ps["losses"]
    pwr=round(ps["wins"]/pdec*100,1) if pdec>0 else 0
    tag="✅" if ps["profit"]>0 else "❌"
    print(f"\n  [{pname.upper()}] {ps['trades']}T | {ps['wins']}W "
          f"{ps['losses']}L {ps['be']}BE | {pwr}% WR | ${ps['profit']:,.0f} {tag}")
    for sname,sp in ps["session_profit"].items():
        if sp!=0:
            print(f"    {sname:<8}: ${sp:,.0f} {'✅' if sp>0 else '❌'}")
    if ps["loss_reasons"]:
        print(f"    Losses:")
        for r_,cnt in sorted(ps["loss_reasons"].items(),key=lambda x:-x[1]):
            print(f"      {r_}: {cnt}x")
print(f"💬 شرح: 'Losses' يفصّل السبب الأقرب لكل خسارة (قد يتكرر سبب واحد\n"
      f"   لعدة صفقات). ASIAN_HIGH/LOW_BROKEN يعني كسر مدى آسيا عكس\n"
      f"   اتجاهنا، BE_REVERSAL يعني السعر رجع بعد لمس نقطة BE — كل زوج\n"
      f"   بأسبابه الخاصة يستحق فلتر منفصل لو تكرر سبب واحد بكثرة.")

print(f"\n{'MONTHLY':^64}")
print(s)
for mk,mpnls in sorted(monthly_results.items()):
    mp=sum(mpnls); mw=sum(1 for p in mpnls if p>0)
    ml=sum(1 for p in mpnls if p<0)
    mwr=round(mw/(mw+ml)*100,1) if (mw+ml)>0 else 0
    tag="✅" if mp>0 else "❌"
    bar="█"*int(mwr/10)
    print(f"  {mk} {bar:<10} {mwr:>5}% | {mw}W {ml}L | ${mp:,.0f} {tag}")
neg_months=[mk for mk,mp in monthly_results.items() if sum(mp)<0]
print(f"💬 شرح: شهر سلبي منفرد (❌) وسط أغلبية ✅ غالباً ظرف سوق استثنائي\n"
      f"   (تقلب أخبار كبير، عطلة، تغيّر نظام سعري) لا خلل بنيوي بالنظام —\n"
      f"   لكن يستحق فحص يدوي لو تكرر. أشهر سلبية حالياً: {neg_months if neg_months else 'لا توجد'}.")

print(f"\n{'FILTERS BLOCKED':^64}")
print(s)
total_b=sum(filter_saves.values())
for k,v in filter_saves.items():
    pct=round(v/max(total_b,1)*100,1)
    print(f"  {k:<14}: {v:>5} ({pct}%)")
print(f"💬 شرح: هذا عدد المرات التي رفض فيها كل فلتر فرصة محتملة قبل أي\n"
      f"   تحقق من confluence. 'session'+'htf'+'consec' عادة الأكبر لأنها\n"
      f"   تُفحص أولاً على كل الشمعات. 'confluence' الصغير نسبياً يعني أغلب\n"
      f"   الفرص تُستبعد بفلاتر أبسط (وقت/اتجاه) قبل الوصول لتقييم الجودة.")
if filter_saves.get("news_blackout",0)==0 and not NEWS_TIMES:
    print(f"⚠️  ملاحظة: فلتر الأخبار الحمراء لم يُفعَّل فعلياً لأن ملف\n"
          f"   {NEWS_CSV_PATH} غير موجود — كل الأرقام أعلاه بدون حماية\n"
          f"   من الأخبار. ضع ملف CSV بعمودين (datetime, impact) لتفعيله.")
else:
    print(f"💬 شرح فلتر الأخبار: {filter_saves.get('news_blackout',0)} فرصة رُفضت\n"
          f"   لأنها كانت ضمن ±{NEWS_BLACKOUT_MIN} دقيقة من خبر عالي التأثير.")

print(f"\n{'TRADES LOG (آخر 50)':^64}")
print(s)
for tr in trades_log[-50:]:
    e="WIN " if tr['result']=="WIN" else("BE  " if tr['result']=="BE" else "LOSS")
    be="[BE]" if tr['be'] else "    "
    sc=f"[I{tr['ict']}S{tr['smc']}W{tr['wyc']}|RR{tr['rr']}|Risk{tr['risk_pct']}%]"
    lr=f" <- {','.join(tr['loss_reasons'])}" if tr['loss_reasons'] else ""
    print(f"  {e}|{tr['date']} {tr['time']}|{tr['asset']} {tr['dir']}"
          f"|{tr['session']:<8}|P{tr['phase']}|{sc}|{tr['exit_type']:<14}|${tr['pnl']:,.0f} {be}{lr}")
print(S)


# ══════════════════════════════════════════════════════════════
#   🆕 V17 ULTIMATE — EXTENDED ANALYTICS ENGINE
#   إضافة خانات تحليل متكاملة دون تغيير منطق الاستراتيجية
# ══════════════════════════════════════════════════════════════

import math
import json
import statistics

# ─────────────────────────────────────────────
#   HELPER: إحصاءات سريعة لقائمة PnL
# ─────────────────────────────────────────────
def _stats(pnls):
    """يحسب إحصاءات أساسية لقائمة PnL مُعطاة."""
    if not pnls:
        return dict(n=0, wins=[], losses=[], wr=0, pf=0, exp=0,
                    avg_w=0, avg_l=0, total=0, net=0)
    w = [p for p in pnls if p > 0]
    l = [p for p in pnls if p < 0]
    dec = len(w) + len(l)
    wr  = len(w) / dec * 100 if dec > 0 else 0
    pf  = sum(w) / abs(sum(l)) if l else 0
    avg_w = sum(w) / len(w) if w else 0
    avg_l = sum(l) / len(l) if l else 0
    exp = (wr/100 * avg_w) + ((1 - wr/100) * avg_l)
    return dict(n=len(pnls), wins=w, losses=l, wr=wr, pf=pf,
                exp=exp, avg_w=avg_w, avg_l=avg_l,
                total=len(pnls), net=sum(pnls))

def _section(title):
    W = 68
    print(f"\n{'═'*W}")
    print(f"  {title}")
    print('═'*W)

def _sub(title):
    print(f"\n  {'─'*60}")
    print(f"  {title}")
    print(f"  {'─'*60}")

# ─────────────────────────────────────────────────────────────
#  1. ADVANCED KPI ENGINE  (150+ مؤشر)
# ─────────────────────────────────────────────────────────────
_section("📊 V17 — ADVANCED KPI ENGINE (150+ INDICATOR)")

pnls_arr = all_pnls   # الـ PnL الكاملة من V16
n_trades  = len(pnls_arr)
wins_arr  = [p for p in pnls_arr if p > 0]
loss_arr  = [p for p in pnls_arr if p < 0]
be_arr    = [p for p in pnls_arr if abs(p) < 1e-6]
dec_n     = len(wins_arr) + len(loss_arr)
total_net = sum(pnls_arr)

# ── Ratios ──────────────────────────────────────
_gross_profit  = sum(wins_arr)
_gross_loss    = abs(sum(loss_arr))
_profit_factor = _gross_profit / _gross_loss if _gross_loss > 0 else 0
_win_rate      = len(wins_arr) / dec_n * 100 if dec_n > 0 else 0
_avg_win       = _gross_profit / len(wins_arr) if wins_arr else 0
_avg_loss      = _gross_loss / len(loss_arr) if loss_arr else 0
_expectancy    = (_win_rate/100 * _avg_win) - ((1-_win_rate/100) * _avg_loss)
_avg_rr_all    = [tr["rr"] for tr in trades_log]
_avg_rr        = statistics.mean(_avg_rr_all) if _avg_rr_all else 0

# ── Drawdown (true running) ──────────────────────
_peak = INITIAL_BALANCE; _max_dd = 0; _dd_periods = []
_in_dd = False; _dd_start_val = INITIAL_BALANCE; _dd_dur = 0
for e in true_equity_curve:
    if e > _peak:
        if _in_dd:
            _dd_periods.append(_dd_dur)
            _in_dd = False; _dd_dur = 0
        _peak = e
    else:
        _max_dd = max(_max_dd, _peak - e)
        _in_dd = True; _dd_dur += 1
if _in_dd:
    _dd_periods.append(_dd_dur)

_avg_dd_pct   = (sum(_dd_periods) / len(_dd_periods)) if _dd_periods else 0
_max_dd_pct_v = _max_dd / INITIAL_BALANCE * 100
_recovery_f   = total_net / _max_dd if _max_dd > 0 else 0

# ── Sharpe / Sortino / Calmar / Omega ──────────
_returns  = [p / INITIAL_BALANCE for p in pnls_arr]
_mean_ret = statistics.mean(_returns) if _returns else 0
_std_ret  = statistics.stdev(_returns) if len(_returns) > 1 else 0
_neg_rets = [r for r in _returns if r < 0]
_down_dev = (sum(r**2 for r in _neg_rets)/len(_neg_rets))**0.5 if _neg_rets else 0

_sharpe   = (_mean_ret / _std_ret) * (252**0.5) if _std_ret > 0 else 0
_sortino  = (_mean_ret / _down_dev) * (252**0.5) if _down_dev > 0 else 0

_all_dates_log = [tr["date"] for tr in trades_log]
_n_years   = 0
if _all_dates_log:
    _d0 = datetime.strptime(min(_all_dates_log), "%Y-%m-%d")
    _d1 = datetime.strptime(max(_all_dates_log), "%Y-%m-%d")
    _span_days = (_d1 - _d0).days
    _n_years   = _span_days / 365.25 if _span_days > 0 else 1

_final_eq  = INITIAL_BALANCE + total_net
_cagr      = ((_final_eq / INITIAL_BALANCE) ** (1 / _n_years) - 1) * 100 if _n_years > 0 else 0
_calmar    = _cagr / _max_dd_pct_v if _max_dd_pct_v > 0 else 0
_mar       = total_net / _max_dd if _max_dd > 0 else 0

# Omega Ratio (threshold = 0)
_omega_gains = sum(r for r in _returns if r > 0)
_omega_loss  = abs(sum(r for r in _returns if r < 0))
_omega       = _omega_gains / _omega_loss if _omega_loss > 0 else 0

# Kelly Criterion
_kc_b = _avg_win / _avg_loss if _avg_loss > 0 else 0
_kelly = (_win_rate/100 - (1-_win_rate/100) / _kc_b) if _kc_b > 0 else 0
_kelly_pct = _kelly * 100

# SQN (System Quality Number)
if len(_returns) > 1:
    _sqn = (_mean_ret / _std_ret) * (n_trades**0.5) if _std_ret > 0 else 0
else:
    _sqn = 0

# VaR / CVaR (95%)
_sorted_r = sorted(_returns)
_var_idx  = int(len(_sorted_r) * 0.05)
_var_95   = _sorted_r[_var_idx] * INITIAL_BALANCE if _sorted_r else 0
_cvar_95  = statistics.mean(_sorted_r[:_var_idx]) * INITIAL_BALANCE if _sorted_r[:_var_idx] else 0

# Skewness / Kurtosis
def _skewness(data):
    n = len(data)
    if n < 3: return 0
    m = statistics.mean(data)
    s = statistics.stdev(data)
    if s == 0: return 0
    return (n / ((n-1)*(n-2))) * sum(((x-m)/s)**3 for x in data)

def _kurtosis(data):
    n = len(data)
    if n < 4: return 0
    m = statistics.mean(data)
    s = statistics.stdev(data)
    if s == 0: return 0
    k = (n*(n+1)/((n-1)*(n-2)*(n-3))) * sum(((x-m)/s)**4 for x in data)
    return k - 3*(n-1)**2/((n-2)*(n-3))

_skew = _skewness(_returns)
_kurt = _kurtosis(_returns)

# Max Consecutive
_cw = _cl = _max_cw = _max_cl = 0
for p in pnls_arr:
    if p > 0:   _cw += 1; _cl = 0; _max_cw = max(_max_cw, _cw)
    elif p < 0: _cl += 1; _cw = 0; _max_cl = max(_max_cl, _cl)
    else:       _cw = _cl = 0

# Time under water
_under_water = sum(1 for e in true_equity_curve if e < INITIAL_BALANCE)
_tuw_pct     = _under_water / len(true_equity_curve) * 100 if true_equity_curve else 0

# Average holding time (bars)
_durations = []
for tr in trades_log:
    # لا يوجد exit_bar مباشرة؛ نستخدم exit_type كمؤشر نسبي
    _durations.append(FOLLOW_WINDOW if tr["exit_type"] == "TIMEOUT_MTM" else FOLLOW_WINDOW // 2)
_avg_hold_bars  = statistics.mean(_durations) if _durations else 0
_avg_hold_hrs   = _avg_hold_bars * 5 / 60   # 5-دقائق شمعة → ساعات

# Drawdown Duration
_max_dd_dur  = max(_dd_periods) if _dd_periods else 0
_avg_dd_dur  = statistics.mean(_dd_periods) if _dd_periods else 0

# ── طباعة KPIs ──────────────────────────────────────────────

_sub("Core Performance KPIs")
_kpis = [
    ("Gross Profit",         f"${_gross_profit:,.2f}"),
    ("Gross Loss",           f"${_gross_loss:,.2f}"),
    ("Net Profit",           f"${total_net:,.2f}"),
    ("Profit Factor",        f"{_profit_factor:.3f}"),
    ("Expectancy",           f"${_expectancy:,.2f}"),
    ("Win Rate",             f"{_win_rate:.2f}%"),
    ("Avg Win",              f"${_avg_win:,.2f}"),
    ("Avg Loss",             f"${_avg_loss:,.2f}"),
    ("Avg RR",               f"{_avg_rr:.2f}"),
    ("Recovery Factor",      f"{_recovery_f:.2f}"),
    ("CAGR",                 f"{_cagr:.2f}%"),
    ("Max Drawdown $",       f"${_max_dd:,.2f}"),
    ("Max Drawdown %",       f"{_max_dd_pct_v:.2f}%"),
    ("Avg DD Duration",      f"{_avg_dd_dur:.1f} bars"),
    ("Max DD Duration",      f"{_max_dd_dur} bars"),
    ("Time Under Water",     f"{_tuw_pct:.1f}%"),
    ("Max Win Streak",       f"{_max_cw}"),
    ("Max Loss Streak",      f"{_max_cl}"),
    ("Avg Holding (bars)",   f"{_avg_hold_bars:.1f}"),
    ("Avg Holding (hrs)",    f"{_avg_hold_hrs:.1f}h"),
]
for name_, val_ in _kpis:
    print(f"    {name_:<28}: {val_}")

_sub("Risk-Adjusted Ratios")
_ratios = [
    ("Sharpe Ratio",         f"{_sharpe:.3f}"),
    ("Sortino Ratio",        f"{_sortino:.3f}"),
    ("Calmar Ratio",         f"{_calmar:.3f}"),
    ("MAR Ratio",            f"{_mar:.3f}"),
    ("Omega Ratio",          f"{_omega:.3f}"),
    ("SQN",                  f"{_sqn:.3f}"),
    ("Kelly Criterion",      f"{_kelly_pct:.2f}%"),
    ("VaR 95% (1-trade)",    f"${_var_95:,.2f}"),
    ("CVaR 95%",             f"${_cvar_95:,.2f}"),
    ("Return Skewness",      f"{_skew:.3f}"),
    ("Return Kurtosis",      f"{_kurt:.3f}"),
]
for name_, val_ in _ratios:
    print(f"    {name_:<28}: {val_}")

_sub("Drawdown Frequency")
print(f"    عدد فترات الـ Drawdown  : {len(_dd_periods)}")
print(f"    أطول فترة (bars)        : {_max_dd_dur}")
print(f"    متوسط الفترة (bars)     : {_avg_dd_dur:.1f}")
print(f"    % الوقت تحت الماء       : {_tuw_pct:.1f}%")

# ─────────────────────────────────────────────────────────────
#  2. DETAILED TRADE ANALYTICS — لكل صفقة  (ملخص إجمالي)
# ─────────────────────────────────────────────────────────────
_section("📋 V17 — DETAILED TRADE ANALYTICS")

# ICT Score لكل صفقة (i+s+w)
_ict_scores_all = [tr["ict"] + tr["smc"] + tr["wyc"] for tr in trades_log]
_conf_scores    = [tr["score"] for tr in trades_log]

# Institutional Score بسيط: (score / 13) * 100   [13 = max ممكن]
MAX_SCORE = 13
_inst_scores = [round(s / MAX_SCORE * 100, 1) for s in _conf_scores]

def _grade(s):
    if s >= 85: return "A+"
    if s >= 70: return "A"
    if s >= 55: return "B"
    if s >= 40: return "C"
    return "D"

_grades = [_grade(s) for s in _inst_scores]
from collections import Counter
_grade_dist = Counter(_grades)

_sub("AI Confidence & Institutional Score Distribution")
print(f"    Avg Institutional Score : {statistics.mean(_inst_scores):.1f}/100")
print(f"    Max Institutional Score : {max(_inst_scores):.1f}/100")
print(f"    Min Institutional Score : {min(_inst_scores):.1f}/100")
print()
for g in ["A+", "A", "B", "C", "D"]:
    cnt = _grade_dist.get(g, 0)
    pct = cnt / n_trades * 100 if n_trades else 0
    bar = "█" * int(pct / 5)
    # WR لكل درجة
    _g_pnls = [trades_log[idx]["pnl"] for idx, gr in enumerate(_grades) if gr == g]
    _g_wr   = sum(1 for p in _g_pnls if p > 0) / len(_g_pnls) * 100 if _g_pnls else 0
    _g_net  = sum(_g_pnls)
    print(f"    Grade {g} : {bar:<15} {cnt:>5}T ({pct:.1f}%) | WR {_g_wr:.1f}% | Net ${_g_net:,.0f}")

_sub("RR Distribution")
_rr_buckets = {}
for tr in trades_log:
    rr_k = f"{tr['rr']:.1f}"
    if rr_k not in _rr_buckets:
        _rr_buckets[rr_k] = []
    _rr_buckets[rr_k].append(tr["pnl"])
for rr_k in sorted(_rr_buckets):
    _bp = _rr_buckets[rr_k]
    _bw = sum(1 for p in _bp if p > 0)
    _bwr = _bw / len(_bp) * 100 if _bp else 0
    print(f"    RR {rr_k}x : {len(_bp):>4}T | WR {_bwr:.1f}% | Net ${sum(_bp):,.0f}")

_sub("Exit Type Detailed Analytics")
_exit_detail = {}
for tr in trades_log:
    et = tr["exit_type"]
    if et not in _exit_detail:
        _exit_detail[et] = []
    _exit_detail[et].append(tr["pnl"])

for et, ep in sorted(_exit_detail.items()):
    s = _stats(ep)
    print(f"    {et:<18}: {s['n']:>4}T | WR {s['wr']:.1f}% | "
          f"PF {s['pf']:.2f} | Avg Win ${s['avg_w']:,.0f} | "
          f"Avg Loss ${s['avg_l']:,.0f} | Net ${s['net']:,.0f}")

# ─────────────────────────────────────────────────────────────
#  3. ICT CONDITION ANALYSIS — درجة لكل شرط
# ─────────────────────────────────────────────────────────────
_section("🔬 V17 — ICT / SMC / WYC CONDITION DEEP ANALYSIS")

_all_conditions_meta = {
    "ICT_SWEEP" : "ICT", "ICT_FVG"   : "ICT", "ICT_OB"    : "ICT",
    "ICT_CISD"  : "ICT", "ICT_PD"    : "ICT",
    "SMC_BOS"   : "SMC", "SMC_CHOCH" : "SMC", "SMC_LIQ"   : "SMC",
    "SMC_IMBAL" : "SMC",
    "WYC_SPRING": "WYC", "WYC_TEST"  : "WYC", "WYC_CAUSE" : "WYC",
    "WYC_EFFORT": "WYC",
}

_sub("Per-Condition Scorecard")
print(f"  {'Condition':<16} {'School':<6} {'Signals':>7} {'Accept':>7} "
      f"{'Reject':>7} {'WR':>6} {'PF':>6} {'Score/100':>9} {'Rec'}")
print(f"  {'─'*16} {'─'*6} {'─'*7} {'─'*7} {'─'*7} {'─'*6} {'─'*6} {'─'*9} {'─'*15}")

for cname, cs in sorted(condition_stats.items(), key=lambda x: -x[1]["wins"]):
    school  = _all_conditions_meta.get(cname, "???")
    total_s = cs["count"]
    wins_s  = cs["wins"]
    rej_s   = n_trades - total_s   # صفقات لم يكن فيها هذا الشرط
    cwr     = wins_s / total_s * 100 if total_s > 0 else 0
    # PF تقريبي بناءً على WR وافتراض avg_win/avg_loss متوسط النظام
    cpf = (cwr/100 * _avg_win) / ((1-cwr/100) * _avg_loss) if _avg_loss > 0 and cwr < 100 else 0
    # Score /100 = WR * 0.6 + (PF/3)*40
    c_score = min(100, round(cwr * 0.6 + min(cpf/3, 1) * 40, 1))
    rec = ("✅ KEEP" if c_score >= 60
           else "⚠️ REVIEW" if c_score >= 45
           else "❌ WEAK")
    print(f"  {cname:<16} {school:<6} {total_s:>7} {wins_s:>7} "
          f"{rej_s:>7} {cwr:>5.1f}% {cpf:>6.2f} {c_score:>9.1f} {rec}")

# ─────────────────────────────────────────────────────────────
#  4. FILTER ANALYSIS — تحليل تأثير كل فلتر
# ─────────────────────────────────────────────────────────────
_section("🔧 V17 — FILTER ANALYSIS")

_total_blocked = sum(filter_saves.values())
_total_passed  = n_trades
_total_seen    = _total_blocked + _total_passed

print(f"\n  {'Filter':<18} {'Blocked':>8} {'%Total':>8} {'Impact Est.'}")
print(f"  {'─'*18} {'─'*8} {'─'*8} {'─'*30}")

_filter_weights = {
    "htf"          : "عالي — يحمي من دخول ضد الاتجاه الكبير",
    "range"        : "متوسط — يُلغي بيئات منخفضة التقلب",
    "session"      : "عالي — يُبقي الصفقات في نوافذ السيولة",
    "maxday"       : "أمان — يحدّ من الإفراط اليومي",
    "consec"       : "حماية — يوقف النزيف عند سلاسل الخسارة",
    "confluence"   : "جودة — يرفض الإشارات الضعيفة",
    "fakeout_dist" : "دقيق — يتجنب الدخول قرب الحافة",
    "candle_confirm": "تأكيد — يشترط شمعة في الاتجاه",
    "big_candle"   : "فلتر تقلب — يتجنب الشمعات الضخمة",
    "no_atr"       : "بيانات — يُسقط شمعات بدون ATR",
    "excluded_hour": "استثناء — ساعات سلبية مؤكدة",
    "news_blackout": "أخبار — حظر ±30د من أخبار حمراء",
}

for fk, fv in sorted(filter_saves.items(), key=lambda x: -x[1]):
    pct = fv / _total_seen * 100 if _total_seen > 0 else 0
    desc = _filter_weights.get(fk, "—")
    bar  = "█" * int(pct / 3)
    print(f"  {fk:<18} {fv:>8,} {pct:>7.1f}% {bar} {desc}")

print(f"\n  إجمالي المرشحات : {_total_seen:,}")
print(f"  مقبول للتداول  : {_total_passed:,} ({_total_passed/_total_seen*100:.1f}%)")
print(f"  محظور بالفلاتر : {_total_blocked:,} ({_total_blocked/_total_seen*100:.1f}%)")

# ─────────────────────────────────────────────────────────────
#  5. SESSION DEEP ANALYSIS
# ─────────────────────────────────────────────────────────────
_section("⏰ V17 — SESSION DEEP ANALYSIS")

_ses_detail = {}
for tr in trades_log:
    s = tr.get("session", "UNKNOWN")
    if s not in _ses_detail:
        _ses_detail[s] = []
    _ses_detail[s].append(tr["pnl"])

print(f"\n  {'Session':<12} {'Trades':>7} {'WR':>7} {'PF':>7} "
      f"{'Exp $':>8} {'Net $':>10} {'Avg Win':>9} {'Avg Loss':>9}")
print(f"  {'─'*12} {'─'*7} {'─'*7} {'─'*7} {'─'*8} {'─'*10} {'─'*9} {'─'*9}")

for s_name, s_pnls in sorted(_ses_detail.items()):
    st = _stats(s_pnls)
    tag = "🏆" if st["net"] == max(x["net"] for x in [_stats(v) for v in _ses_detail.values()]) else ""
    print(f"  {s_name:<12} {st['n']:>7} {st['wr']:>6.1f}% {st['pf']:>7.2f} "
          f"{st['exp']:>8,.0f} {st['net']:>10,.0f} {st['avg_w']:>9,.0f} "
          f"{st['avg_l']:>9,.0f} {tag}")

# ─────────────────────────────────────────────────────────────
#  6. TIME ANALYSIS — Hour / Day / Week / Month / Quarter
# ─────────────────────────────────────────────────────────────
_section("📅 V17 — TIME ANALYSIS (Hour / Day / Week / Month / Quarter)")

# ── By Hour ─────────────────────────────────────────────────
_sub("By Hour")
print(f"  {'Hour':>5} {'T':>5} {'WR':>7} {'PF':>7} {'Exp $':>8} {'Net $':>10}")
for h in sorted(hour_results.keys()):
    st = _stats(hour_results[h])
    flag = "✅" if st["net"] > 0 else "❌"
    print(f"  {h:02d}:00 {st['n']:>5} {st['wr']:>6.1f}% {st['pf']:>7.2f} "
          f"{st['exp']:>8,.0f} {st['net']:>10,.0f} {flag}")

# ── By Day of Week ───────────────────────────────────────────
_sub("By Day of Week")
_dow_results = {}
for tr in trades_log:
    d = datetime.strptime(tr["date"], "%Y-%m-%d").strftime("%A")
    if d not in _dow_results: _dow_results[d] = []
    _dow_results[d].append(tr["pnl"])

_day_order = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
print(f"  {'Day':<12} {'T':>5} {'WR':>7} {'PF':>7} {'Net $':>10}")
for d in _day_order:
    if d not in _dow_results: continue
    st = _stats(_dow_results[d])
    flag = "✅" if st["net"] > 0 else "❌"
    print(f"  {d:<12} {st['n']:>5} {st['wr']:>6.1f}% {st['pf']:>7.2f} "
          f"{st['net']:>10,.0f} {flag}")

# ── By Month ─────────────────────────────────────────────────
_sub("By Month")
print(f"  {'Month':<10} {'T':>5} {'WR':>7} {'PF':>7} {'Exp $':>8} {'Net $':>10}")
for mk, mp in sorted(monthly_results.items()):
    st = _stats(mp)
    flag = "✅" if st["net"] > 0 else "❌"
    print(f"  {mk:<10} {st['n']:>5} {st['wr']:>6.1f}% {st['pf']:>7.2f} "
          f"{st['exp']:>8,.0f} {st['net']:>10,.0f} {flag}")

# ── By Quarter ───────────────────────────────────────────────
_sub("By Quarter")
_qtr_results = {}
for mk, mp in monthly_results.items():
    try:
        y, m = mk.split("-")
        q = f"{y}-Q{(int(m)-1)//3+1}"
    except:
        q = "UNKNOWN"
    if q not in _qtr_results: _qtr_results[q] = []
    _qtr_results[q].extend(mp)
print(f"  {'Quarter':<10} {'T':>5} {'WR':>7} {'PF':>7} {'Net $':>10}")
for q in sorted(_qtr_results):
    st = _stats(_qtr_results[q])
    flag = "✅" if st["net"] > 0 else "❌"
    print(f"  {q:<10} {st['n']:>5} {st['wr']:>6.1f}% {st['pf']:>7.2f} "
          f"{st['net']:>10,.0f} {flag}")

# ─────────────────────────────────────────────────────────────
#  7. PER-PAIR DEEP ANALYSIS
# ─────────────────────────────────────────────────────────────
_section("💱 V17 — PER-PAIR DEEP ANALYSIS")

_pair_detail = {}
for tr in trades_log:
    p = tr["asset"]
    if p not in _pair_detail: _pair_detail[p] = []
    _pair_detail[p].append(tr)

for p_name, p_trades in _pair_detail.items():
    _sub(f"Pair: {p_name}")
    p_pnls = [t["pnl"] for t in p_trades]
    st = _stats(p_pnls)
    print(f"  Trades: {st['n']} | WR: {st['wr']:.1f}% | PF: {st['pf']:.2f} | "
          f"Net: ${st['net']:,.0f} | Exp: ${st['exp']:,.0f}")

    # Per-session
    _p_ses = {}
    for t in p_trades:
        s = t.get("session","?")
        if s not in _p_ses: _p_ses[s] = []
        _p_ses[s].append(t["pnl"])
    best_ses  = max(_p_ses, key=lambda x: sum(_p_ses[x])) if _p_ses else "—"
    worst_ses = min(_p_ses, key=lambda x: sum(_p_ses[x])) if _p_ses else "—"
    print(f"  Best Session: {best_ses} | Worst Session: {worst_ses}")

    # Per-month best/worst
    _p_mon = {}
    for t in p_trades:
        mk = t["date"][:7]
        if mk not in _p_mon: _p_mon[mk] = []
        _p_mon[mk].append(t["pnl"])
    if _p_mon:
        best_m  = max(_p_mon, key=lambda x: sum(_p_mon[x]))
        worst_m = min(_p_mon, key=lambda x: sum(_p_mon[x]))
        print(f"  Best Month:   {best_m} (${sum(_p_mon[best_m]):,.0f}) | "
              f"Worst Month: {worst_m} (${sum(_p_mon[worst_m]):,.0f})")

    # Average RR
    _p_rr = [t["rr"] for t in p_trades]
    print(f"  Avg RR: {statistics.mean(_p_rr):.2f} | "
          f"Risk Pct Avg: {statistics.mean([t['risk_pct'] for t in p_trades]):.2f}%")

# ─────────────────────────────────────────────────────────────
#  8. DIRECTION DEEP ANALYSIS
# ─────────────────────────────────────────────────────────────
_section("📐 V17 — DIRECTION DEEP ANALYSIS")

_dir_detail = {}
for tr in trades_log:
    d = tr["dir"]
    if d not in _dir_detail: _dir_detail[d] = []
    _dir_detail[d].append(tr["pnl"])

print(f"\n  {'Direction':<10} {'Trades':>7} {'WR':>7} {'PF':>7} "
      f"{'Exp $':>8} {'Net $':>10}")
for d, dp in sorted(_dir_detail.items()):
    st = _stats(dp)
    flag = "✅" if st["net"] > 0 else "❌"
    print(f"  {d:<10} {st['n']:>7} {st['wr']:>6.1f}% {st['pf']:>7.2f} "
          f"{st['exp']:>8,.0f} {st['net']:>10,.0f} {flag}")

# ─────────────────────────────────────────────────────────────
#  9. ROLLING ANALYTICS — Rolling WR / PF / Expectancy
# ─────────────────────────────────────────────────────────────
_section("📈 V17 — ROLLING ANALYTICS (Window=20 trades)")

ROLL_WIN = 20
_rolling_pf  = []
_rolling_wr  = []
_rolling_exp = []

for idx in range(ROLL_WIN, len(pnls_arr)+1):
    _w = pnls_arr[idx-ROLL_WIN:idx]
    st = _stats(_w)
    _rolling_pf.append(st["pf"])
    _rolling_wr.append(st["wr"])
    _rolling_exp.append(st["exp"])

if _rolling_pf:
    print(f"\n  Rolling PF (20T window):")
    print(f"    Min: {min(_rolling_pf):.2f} | Max: {max(_rolling_pf):.2f} | "
          f"Avg: {statistics.mean(_rolling_pf):.2f}")
    print(f"  Rolling WR:")
    print(f"    Min: {min(_rolling_wr):.1f}% | Max: {max(_rolling_wr):.1f}% | "
          f"Avg: {statistics.mean(_rolling_wr):.1f}%")
    print(f"  Rolling Expectancy:")
    print(f"    Min: ${min(_rolling_exp):,.0f} | Max: ${max(_rolling_exp):,.0f} | "
          f"Avg: ${statistics.mean(_rolling_exp):,.0f}")

    # Stability Score (كلما قل التباين كلما ارتفع الاستقرار)
    _pf_std   = statistics.stdev(_rolling_pf) if len(_rolling_pf) > 1 else 0
    _stability = max(0, 100 - _pf_std * 30)
    print(f"\n  Rolling PF Stability Score: {_stability:.1f}/100")

# ─────────────────────────────────────────────────────────────
# 10. MONTE CARLO SIMULATION (1000 runs)
# ─────────────────────────────────────────────────────────────
_section("🎲 V17 — MONTE CARLO SIMULATION (1000 runs)")

import random
random.seed(42)

MC_RUNS  = 1000
_mc_dd   = []
_mc_net  = []
_mc_streak_l = []

for _ in range(MC_RUNS):
    sample   = random.choices(pnls_arr, k=len(pnls_arr))
    eq = INITIAL_BALANCE
    pk = INITIAL_BALANCE
    mx_dd_mc = 0
    streak = 0; max_streak = 0
    for p in sample:
        eq += p
        pk = max(pk, eq)
        mx_dd_mc = max(mx_dd_mc, pk - eq)
        streak = streak + 1 if p < 0 else 0
        max_streak = max(max_streak, streak)
    _mc_dd.append(mx_dd_mc)
    _mc_net.append(eq - INITIAL_BALANCE)
    _mc_streak_l.append(max_streak)

_mc_dd.sort()
_mc_net.sort()

_mc_worst_dd   = max(_mc_dd)
_mc_avg_dd     = statistics.mean(_mc_dd)
_mc_dd_p95     = _mc_dd[int(MC_RUNS * 0.95)]
_mc_ruin       = sum(1 for n in _mc_net if n < -INITIAL_BALANCE * MAX_TOTAL_DD_PCT) / MC_RUNS * 100
_mc_worst_str  = max(_mc_streak_l)
_mc_avg_cagr   = (((INITIAL_BALANCE + statistics.mean(_mc_net)) / INITIAL_BALANCE)
                   ** (1 / max(_n_years, 0.1)) - 1) * 100

print(f"\n  Simulations     : {MC_RUNS:,}")
print(f"  Worst DD        : ${_mc_worst_dd:,.0f} ({_mc_worst_dd/INITIAL_BALANCE*100:.2f}%)")
print(f"  Avg DD          : ${_mc_avg_dd:,.0f} ({_mc_avg_dd/INITIAL_BALANCE*100:.2f}%)")
print(f"  95th pct DD     : ${_mc_dd_p95:,.0f} ({_mc_dd_p95/INITIAL_BALANCE*100:.2f}%)")
print(f"  Worst Loss Str  : {_mc_worst_str}")
print(f"  Probability Ruin: {_mc_ruin:.2f}%")
print(f"  Expected CAGR   : {_mc_avg_cagr:.2f}%")
print(f"\n  تفسير:")
if _mc_ruin < 5:
    print(f"  ✅ احتمال الانفجار منخفض جداً ({_mc_ruin:.1f}%) — النظام متين إحصائياً.")
elif _mc_ruin < 15:
    print(f"  ⚠️  احتمال الانفجار معتدل ({_mc_ruin:.1f}%) — تراقب إدارة المخاطر.")
else:
    print(f"  ❌ احتمال الانفجار مرتفع ({_mc_ruin:.1f}%) — راجع حجم المخاطرة فوراً.")

# ─────────────────────────────────────────────────────────────
# 11. OVERFITTING DETECTION
# ─────────────────────────────────────────────────────────────
_section("🔍 V17 — OVERFITTING DETECTION")

# الفرق بين IS و OOS كمقياس للـ Curve Fit
if _all_dates_log:
    _is_pnls  = [tr["pnl"] for tr in is_trades]  if 'is_trades'  in dir() else []
    _oos_pnls = [tr["pnl"] for tr in oos_trades] if 'oos_trades' in dir() else []

    if _is_pnls and _oos_pnls:
        _is_st  = _stats(_is_pnls)
        _oos_st = _stats(_oos_pnls)

        # Curve Fit % = انخفاض WR من IS إلى OOS
        _cf_pct  = max(0, _is_st["wr"] - _oos_st["wr"])
        _pf_deg  = max(0, _is_st["pf"] - _oos_st["pf"])

        # Sensitivity Score (انحراف نسبي)
        _sens = abs(_is_st["exp"] - _oos_st["exp"]) / (abs(_is_st["exp"]) + 1) * 100

        # Overfitting Risk
        if _cf_pct > 15 or _pf_deg > 1.0:
            _of_risk = "🔴 HIGH"
        elif _cf_pct > 8 or _pf_deg > 0.5:
            _of_risk = "🟡 MEDIUM"
        else:
            _of_risk = "🟢 LOW"

        _param_stability = max(0, 100 - _cf_pct * 2 - _pf_deg * 10)

        print(f"\n  Curve Fit %           : {_cf_pct:.1f}%  (IS WR {_is_st['wr']:.1f}% → OOS WR {_oos_st['wr']:.1f}%)")
        print(f"  PF Degradation        : {_pf_deg:.2f}  (IS PF {_is_st['pf']:.2f} → OOS PF {_oos_st['pf']:.2f})")
        print(f"  Expectancy Sensitivity: {_sens:.1f}%")
        print(f"  Parameter Stability   : {_param_stability:.1f}/100")
        print(f"  Overfitting Risk      : {_of_risk}")
    else:
        print("\n  لا توجد بيانات كافية لـ IS/OOS.")

# ─────────────────────────────────────────────────────────────
# 12. CORRELATION ANALYSIS (بين الأزواج)
# ─────────────────────────────────────────────────────────────
_section("🔗 V17 — PAIR CORRELATION ANALYSIS")

# بناء سلسلة PnL يومية لكل زوج
_pair_daily = {}
for tr in trades_log:
    p = tr["asset"]; d = tr["date"]
    if p not in _pair_daily: _pair_daily[p] = {}
    _pair_daily[p][d] = _pair_daily[p].get(d, 0) + tr["pnl"]

_pair_names = sorted(_pair_daily.keys())
_all_days   = sorted(set(d for pd_ in _pair_daily.values() for d in pd_))

def _corr(x, y):
    n = len(x)
    if n < 2: return 0
    mx, my = statistics.mean(x), statistics.mean(y)
    num = sum((a-mx)*(b-my) for a, b in zip(x, y))
    den = (sum((a-mx)**2 for a in x) * sum((b-my)**2 for b in y))**0.5
    return num / den if den > 0 else 0

print(f"\n  {'Pair A':<10} {'Pair B':<10} {'Corr':>8}  {'Risk'}")
print(f"  {'─'*10} {'─'*10} {'─'*8}  {'─'*20}")

for i, pa in enumerate(_pair_names):
    for pb in _pair_names[i+1:]:
        _xa = [_pair_daily[pa].get(d, 0) for d in _all_days]
        _xb = [_pair_daily[pb].get(d, 0) for d in _all_days]
        _c  = _corr(_xa, _xb)
        risk = ("🔴 HIGH (تجنب دخول متزامن)" if abs(_c) > 0.7
                else "🟡 MED" if abs(_c) > 0.4
                else "🟢 LOW")
        print(f"  {pa:<10} {pb:<10} {_c:>8.3f}  {risk}")

print(f"\n  💡 إذا تجاوز الارتباط 0.7 بين زوجين → خفّض حجم الصفقة الثانية إلى 50%")
print(f"     أو تجنب الدخول على كليهما في نفس الوقت لتقليل التعرض الفعلي.")

# ─────────────────────────────────────────────────────────────
# 13. RECOMMENDATION ENGINE  🤖
# ─────────────────────────────────────────────────────────────
_section("🤖 V17 — RECOMMENDATION ENGINE")

def _best_worst(d, key_fn, label):
    items = [(k, key_fn(v)) for k, v in d.items() if v]
    if not items: return "—", "—"
    best  = max(items, key=lambda x: x[1])
    worst = min(items, key=lambda x: x[1])
    return f"{best[0]} ({best[1]:.1f}{label})", f"{worst[0]} ({worst[1]:.1f}{label})"

# Best/Worst Pair
_pair_nets = {p: sum(t["pnl"] for t in pts) for p, pts in _pair_detail.items()}
_bp = max(_pair_nets, key=_pair_nets.get) if _pair_nets else "—"
_wp = min(_pair_nets, key=_pair_nets.get) if _pair_nets else "—"

# Best/Worst Session
_ses_nets = {s: sum(v) for s, v in _ses_detail.items()}
_bs  = max(_ses_nets, key=_ses_nets.get) if _ses_nets else "—"
_ws  = min(_ses_nets, key=_ses_nets.get) if _ses_nets else "—"

# Best/Worst Hour
_hr_nets = {h: sum(v) for h, v in hour_results.items()}
_bh  = max(_hr_nets, key=_hr_nets.get) if _hr_nets else "—"
_wh  = min(_hr_nets, key=_hr_nets.get) if _hr_nets else "—"

# Best/Worst ICT Condition
_cond_nets = {c: cs["wins"] / cs["count"] * 100 if cs["count"] > 0 else 0
              for c, cs in condition_stats.items()}
_bc = max(_cond_nets, key=_cond_nets.get) if _cond_nets else "—"
_wc = min(_cond_nets, key=_cond_nets.get) if _cond_nets else "—"

# Best RR
_rr_nets = {rr_k: sum(v) for rr_k, v in _rr_buckets.items()}
_brr = max(_rr_nets, key=_rr_nets.get) if _rr_nets else "—"

# Best Filter (أقل عدد من الحظر = يقبل أكثر مع الحفاظ على الجودة)
_filt_ratio = {k: n_trades/(n_trades+v) for k,v in filter_saves.items() if v>0}
_bfilt = max(_filt_ratio, key=_filt_ratio.get) if _filt_ratio else "—"
_wfilt = min(_filt_ratio, key=_filt_ratio.get) if _filt_ratio else "—"

print(f"""
  ┌{'─'*60}┐
  │  📊 ملخص التوصيات التلقائية                           │
  ├{'─'*60}┤
  │  Best Pair       : {str(_bp):<40}│
  │  Worst Pair      : {str(_wp):<40}│
  │  Best Session    : {str(_bs):<40}│
  │  Worst Session   : {str(_ws):<40}│
  │  Best Hour       : {f'{_bh:02d}:00':<40}│
  │  Worst Hour      : {f'{_wh:02d}:00':<40}│
  │  Best Setup      : {str(_bc):<40}│
  │  Weakest Setup   : {str(_wc):<40}│
  │  Best RR         : {str(_brr)+'x':<40}│
  │  Key Filter      : {str(_bfilt):<40}│
  └{'─'*60}┘""")

# توصيات عملية
_sub("Practical Recommendations")

# 1. Drawdown
if _max_dd_pct_v > 7:
    print(f"  🔴 DD عالٍ ({_max_dd_pct_v:.1f}%) → فكّر في تخفيض حجم المخاطرة أو "
          f"إضافة فلتر أقوى على CONFLUENCE")
else:
    print(f"  ✅ DD تحت السيطرة ({_max_dd_pct_v:.1f}%)")

# 2. Win Rate
if _win_rate < 45:
    print(f"  ⚠️  WR ({_win_rate:.1f}%) دون 45% → راجع شروط الدخول، "
          f"أو ارفع الحد الأدنى من CONFLUENCE")
else:
    print(f"  ✅ WR ({_win_rate:.1f}%) جيد")

# 3. Profit Factor
if _profit_factor < 1.5:
    print(f"  ⚠️  PF ({_profit_factor:.2f}) ضعيف → زيادة نسبة TP الجزئي أو رفع RR الهدف")
else:
    print(f"  ✅ PF ({_profit_factor:.2f}) قوي")

# 4. SQN
if _sqn > 2:
    print(f"  ✅ SQN ({_sqn:.2f}) ممتاز — النظام ذو جودة تداول عالية")
elif _sqn > 1:
    print(f"  🟡 SQN ({_sqn:.2f}) مقبول — هناك مجال للتحسين")
else:
    print(f"  🔴 SQN ({_sqn:.2f}) ضعيف — مراجعة شاملة مطلوبة")

# 5. Kelly
if _kelly_pct > 0:
    print(f"  💡 Kelly الأمثل: {_kelly_pct:.1f}% "
          f"(مقارنة بمخاطرتك الحالية {RISK_LADDER[RISK_START_IDX]*100:.2f}%)")
    if _kelly_pct < RISK_LADDER[RISK_START_IDX]*100:
        print(f"     ← مخاطرتك أعلى من Kelly → فكّر في التخفيض")
else:
    print(f"  ⚠️  Kelly سلبي ({_kelly_pct:.1f}%) — النظام لا يتوقع ربحاً صافياً بهذه المعادلة")

# 6. Correlation Warning
print(f"\n  📌 راجع قسم Correlation — أي ارتباط > 0.7 بين زوجين")
print(f"     يعني دخولهما معاً يضاعف المخاطرة الفعلية دون إضافة تنوع حقيقي.")

# 7. Overfitting
if '_of_risk' in dir():
    print(f"\n  🔬 Overfitting Risk: {_of_risk}")

# ─────────────────────────────────────────────────────────────
# 14. EXPORT — JSON + CSV
# ─────────────────────────────────────────────────────────────
_section("💾 V17 — DATA EXPORT (JSON + CSV)")

# JSON Summary
_json_summary = {
    "version"      : "V17 Ultimate Institutional Edition",
    "generated_at" : datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "kpis": {
        "net_profit"    : round(total_net, 2),
        "win_rate"      : round(_win_rate, 2),
        "profit_factor" : round(_profit_factor, 3),
        "expectancy"    : round(_expectancy, 2),
        "max_drawdown"  : round(_max_dd, 2),
        "max_dd_pct"    : round(_max_dd_pct_v, 2),
        "sharpe"        : round(_sharpe, 3),
        "sortino"       : round(_sortino, 3),
        "calmar"        : round(_calmar, 3),
        "omega"         : round(_omega, 3),
        "sqn"           : round(_sqn, 3),
        "kelly_pct"     : round(_kelly_pct, 2),
        "cagr"          : round(_cagr, 2),
        "var_95"        : round(_var_95, 2),
        "cvar_95"       : round(_cvar_95, 2),
        "skewness"      : round(_skew, 4),
        "kurtosis"      : round(_kurt, 4),
        "mc_ruin_pct"   : round(_mc_ruin, 2),
        "mc_avg_cagr"   : round(_mc_avg_cagr, 2),
    },
    "recommendations": {
        "best_pair"    : str(_bp),
        "worst_pair"   : str(_wp),
        "best_session" : str(_bs),
        "worst_session": str(_ws),
        "best_hour"    : f"{_bh:02d}:00",
        "worst_hour"   : f"{_wh:02d}:00",
        "best_setup"   : str(_bc),
        "weakest_setup": str(_wc),
        "best_rr"      : str(_brr),
    },
    "trades": trades_log,
}

_json_path = os.path.expanduser("~/V17_results.json")
try:
    with open(_json_path, "w", encoding="utf-8") as f:
        json.dump(_json_summary, f, ensure_ascii=False, indent=2)
    print(f"\n  ✅ JSON  → {_json_path}")
except Exception as e:
    print(f"\n  ❌ JSON export فشل: {e}")

# CSV Trades
_csv_path = os.path.expanduser("~/V17_trades.csv")
try:
    _df_out = pd.DataFrame(trades_log)
    _df_out["inst_score"] = [round(s/MAX_SCORE*100,1) for s in _df_out["score"]]
    _df_out["grade"]      = [_grade(s) for s in _df_out["inst_score"]]
    _df_out.to_csv(_csv_path, index=False, encoding="utf-8-sig")
    print(f"  ✅ CSV   → {_csv_path}")
except Exception as e:
    print(f"  ❌ CSV export فشل: {e}")

# ─────────────────────────────────────────────────────────────
# 15. FINAL SUMMARY BANNER
# ─────────────────────────────────────────────────────────────
W = 68
print(f"\n{'═'*W}")
print(f"  🏆  V17 ULTIMATE INSTITUTIONAL EDITION — COMPLETE")
print(f"{'═'*W}")
print(f"  Net Profit    : ${total_net:>12,.2f}")
print(f"  Win Rate      : {_win_rate:>11.2f}%")
print(f"  Profit Factor : {_profit_factor:>12.3f}")
print(f"  Sharpe        : {_sharpe:>12.3f}")
print(f"  Sortino       : {_sortino:>12.3f}")
print(f"  Calmar        : {_calmar:>12.3f}")
print(f"  SQN           : {_sqn:>12.3f}")
print(f"  Kelly %       : {_kelly_pct:>11.2f}%")
print(f"  Max DD        : ${_max_dd:>11,.2f}  ({_max_dd_pct_v:.2f}%)")
print(f"  Monte Carlo Ruin : {_mc_ruin:>8.2f}%")
print(f"  CAGR          : {_cagr:>11.2f}%")
print(f"{'═'*W}")
