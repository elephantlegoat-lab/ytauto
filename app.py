import os, json, math, random, tempfile, re, calendar, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from flask import Flask, request, session, redirect, render_template_string
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024**3
BASE = os.environ["BASE_URL"].rstrip("/")
PASSWORD = os.environ["APP_PASSWORD"]
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Paris"))
DATA = os.environ.get("DATA_DIR", "data")
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly",
          "https://www.googleapis.com/auth/yt-analytics.readonly"]
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
os.makedirs(DATA + "/chaines", exist_ok=True)
DEFAUT_TAGS = ["#asmr", "#shorts", "#asmrshorts", "#asmrman", "#fyp", "#satisfying", "#relax", "#relaxation",
               "#whispering", "#chuchotement", "#visualasmr", "#satisfyingvideo", "#frissonsgarantis", "#dormir", "#detente"]
OBLIG_TAGS = ["#asmr", "#shorts"]
EXTRA_TAGS = ["#asmrsleep", "#asmrtriggers", "#asmrsounds", "#tingles", "#relaxing", "#sleep", "#asmrcommunity",
              "#oddlysatisfying", "#asmrvideo", "#asmrtapping", "#asmrscratching", "#asmrfrance", "#asmrfr", "#triggers",
              "#brainmelt", "#asmrnomusic", "#calm", "#sleepaid", "#asmrlover", "#satisfyingsounds", "#crunchy",
              "#mouthsounds", "#asmrmic", "#tapping", "#relaxingsounds", "#stressrelief", "#chill", "#asmrfood", "#slime"]
NB_TAGS = 15
MIN_APPRENTISSAGE = 8  # en dessous : on explore au hasard

# ---------- chaînes (une par fichier) ----------
def _p(cid): return f"{DATA}/chaines/{cid}.json"
def charger(cid):
    return json.load(open(_p(cid))) if cid and os.path.exists(_p(cid)) else None
def sauver(cid, d): json.dump(d, open(_p(cid), "w"))
def liste():
    out = []
    for f in sorted(os.listdir(DATA + "/chaines")):
        if f.endswith(".json"):
            out.append((f[:-5], charger(f[:-5])["nom"]))
    return out
def active():
    ids = [c for c, _ in liste()]
    return session.get("cid") if session.get("cid") in ids else (ids[0] if ids else None)

def _env(k): return [x.strip() for x in os.environ.get(k, "").split("|") if x.strip()]
def config(cid):
    c = {"titres": _env("TITRES"), "oblig": OBLIG_TAGS, "pool": _env("HASHTAGS_TEST"),
         "heures": [12, 18, 20], "apprentissage": True}
    c.update((charger(cid) or {}).get("config", {}))
    if not c["oblig"]: c["oblig"] = OBLIG_TAGS
    return c

def creds_for(cid):
    d = charger(cid)
    c = Credentials.from_authorized_user_info(json.loads(d["token"]), SCOPES)
    if not c.valid and c.refresh_token:
        c.refresh(Request())
        d["token"] = c.to_json(); sauver(cid, d)
    return c

def yt_for(cid): return build("youtube", "v3", credentials=creds_for(cid))

def flow():
    cfg = {"web": {"client_id": os.environ["GOOGLE_CLIENT_ID"],
                   "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                   "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                   "token_uri": "https://oauth2.googleapis.com/token"}}
    return Flow.from_client_config(cfg, SCOPES, redirect_uri=BASE + "/oauth/callback")

# ---------- historique YouTube (l'apprentissage se lit directement sur ta chaîne) ----------
def historique(yt):
    up = yt.channels().list(part="contentDetails", mine=True).execute()["items"][0][
        "contentDetails"]["relatedPlaylists"]["uploads"]
    ids = [i["contentDetails"]["videoId"] for i in yt.playlistItems().list(
        part="contentDetails", playlistId=up, maxResults=50).execute().get("items", [])]
    items = yt.videos().list(part="snippet,statistics,status", id=",".join(ids)).execute().get("items", []) if ids else []
    out = []
    for v in items:
        s, st, stt = v["snippet"], v.get("statistics", {}), v["status"]
        pub = stt.get("publishAt") or s["publishedAt"]
        out.append(dict(id=v["id"], titre=s["title"], desc=s.get("description", ""),
                        pub=datetime.fromisoformat(pub.replace("Z", "+00:00")),
                        vues=int(st.get("viewCount", 0)), likes=int(st.get("likeCount", 0)),
                        statut=stt["privacyStatus"],
                        img=s.get("thumbnails", {}).get("medium", {}).get("url", "")))
    return sorted(out, key=lambda v: v["pub"], reverse=True)

def score(v): return math.log1p(v["vues"]) + 0.5 * math.log1p(v["likes"])

def mesurees(hist):
    lim = datetime.now(timezone.utc) - timedelta(hours=48)
    return [v for v in hist if v["statut"] == "public" and v["pub"] <= lim]

def stats_par(vids, cles):
    g = {}
    for v in vids:
        for k in cles(v):
            g.setdefault(k, []).append(score(v))
    return {k: (len(s), sum(s) / len(s)) for k, s in g.items()}

def globales(vids):
    s = [score(v) for v in vids]
    if not s: return 0.0, 1.0
    m = sum(s) / len(s)
    return m, max(math.sqrt(sum((x - m) ** 2 for x in s) / len(s)), 0.5)

def tirages(bras, st, m0, s0):
    """Thompson sampling : ce qui marche sort plus souvent, le peu testé reste exploré."""
    def t(b):
        n, m = st.get(b, (0, m0))
        return random.gauss((n * m + m0) / (n + 1), s0 / math.sqrt(n + 1))
    return sorted(bras, key=t, reverse=True)

def hashtags_de(v): return [h.lower() for h in re.findall(r"#\w+", v["desc"])]

def choisir(hist, cfg):
    vids = mesurees(hist)
    appr = cfg["apprentissage"] and len(vids) >= MIN_APPRENTISSAGE
    m0, s0 = globales(vids)
    titre = None
    if cfg["titres"]:
        cand = [t for t in cfg["titres"] if not hist or t != hist[0]["titre"]] or cfg["titres"]
        titre = tirages(cand, stats_par(vids, lambda v: [v["titre"]]), m0, s0)[0] if appr else random.choice(cand)
    norm = lambda h: h.lower() if h.startswith("#") else "#" + h.lower()
    oblig = [norm(h) for h in cfg["oblig"]]
    vus = [h for v in hist for h in hashtags_de(v)]
    cand = [h for h in dict.fromkeys([norm(h) for h in DEFAUT_TAGS + EXTRA_TAGS + cfg["pool"]] + vus) if h not in oblig]
    n = max(0, NB_TAGS - len(oblig))
    if appr:
        pris_tags = tirages(cand, stats_par(vids, hashtags_de), m0, s0)[:n]
    else:  # phase de test : on garde l'essentiel de ta liste et on teste 4 nouveaux hashtags
        base = [h for h in DEFAUT_TAGS if norm(h) not in oblig][:n]
        base = [norm(h) for h in base]
        random.shuffle(base)
        base = base[:max(0, len(base) - 4)]
        reste = [h for h in cand if h not in base]
        random.shuffle(reste)
        pris_tags = (base + reste)[:n]
    heures = tirages(cfg["heures"], stats_par(vids, lambda v: [v["pub"].astimezone(TZ).hour]), m0, s0) if appr \
        else random.sample(cfg["heures"], len(cfg["heures"]))
    pris = {v["pub"].astimezone(TZ).date() for v in hist if v["pub"] > datetime.now(timezone.utc)}
    now = datetime.now(TZ)
    quand = None
    for d in range(0, 15):
        t = (now + timedelta(days=d)).replace(hour=int(heures[0]), minute=0, second=0, microsecond=0)
        if t >= now + timedelta(hours=1) and t.date() not in pris:
            quand = t; break
    quand = quand or (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
    return titre, oblig + pris_tags, quand.astimezone(timezone.utc), appr

def gemini(prompt):
    key = os.environ.get("GEMINI_API_KEY")
    if not key: return None
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=30))
        return r["candidates"][0]["content"]["parts"][0]["text"].strip().strip('"')[:100]
    except Exception:
        return None

def titre_ia(desc):
    return gemini(f"Écris UN seul titre YouTube accrocheur (max 70 caractères) pour : {desc}\nRéponds uniquement par le titre.") \
        or desc[:90]

def titre_inspire(hist):
    """Analyse tes titres : prend ceux qui marchent le mieux et en écrit un nouveau, proche mais pas identique."""
    v = sorted(mesurees(hist), key=score, reverse=True)
    if len(v) < 3: return None
    p = ("Tu aides une chaîne YouTube ASMR francophone.\nTitres qui ont le MIEUX marché :\n- "
         + "\n- ".join(x["titre"] for x in v[:8]) + "\n\nTitres qui ont le MOINS marché :\n- "
         + "\n- ".join(x["titre"] for x in v[-3:])
         + "\n\nÉcris UN nouveau titre dans l'esprit des meilleurs (mêmes mots-clés, même ton, même longueur, même structure) "
           "mais différent : ne copie aucun titre. Max 70 caractères. Réponds uniquement par le titre.")
    t = gemini(p)
    return t if t and t.lower() not in {x["titre"].lower() for x in hist} else None

SOURCES = {"SHORTS": "Shorts", "YT_SEARCH": "Recherche YouTube", "SUGGESTED_VIDEO": "Vidéos suggérées",
           "BROWSE": "Accueil YouTube", "EXT_URL": "Sites externes", "NO_LINK_OTHER": "Direct / autre",
           "NOTIFICATION": "Notifications", "YT_CHANNEL": "Page de chaîne", "PLAYLIST": "Playlists",
           "YT_OTHER_PAGE": "Autres pages YouTube", "END_SCREEN": "Fin de vidéo", "HASHTAGS": "Hashtags"}

def analytique(cid, yt, j):
    ya = build("youtubeAnalytics", "v2", credentials=creds_for(cid))
    fin = datetime.now(TZ).date(); deb = fin - timedelta(days=j)
    q = lambda **k: ya.reports().query(ids="channel==MINE", startDate=str(deb), endDate=str(fin), **k).execute().get("rows", [])
    jour = q(metrics="views,estimatedMinutesWatched,likes,subscribersGained,subscribersLost", dimensions="day", sort="day")
    top = q(metrics="views,estimatedMinutesWatched", dimensions="video", sort="-views", maxResults=8)
    src = q(metrics="views", dimensions="insightTrafficSourceType", sort="-views", maxResults=8)
    noms = {}
    if top:
        for x in yt.videos().list(part="snippet", id=",".join(r[0] for r in top)).execute().get("items", []):
            noms[x["id"]] = x["snippet"]["title"]
    S = lambda i: [r[i] for r in jour]
    return dict(labels=[r[0][8:] + "/" + r[0][5:7] for r in jour], vues=S(1), heures=[round(m / 60, 2) for m in S(2)],
                likes=S(3), abos=[a - b for a, b in zip(S(4), S(5))],
                tot=dict(vues=sum(S(1)), heures=round(sum(S(2)) / 60, 1), likes=sum(S(3)), abos=sum(S(4)) - sum(S(5))),
                top=[(noms.get(r[0], r[0]), r[1], round(r[2] / 60, 1)) for r in top],
                src=[(SOURCES.get(r[0], r[0]), r[1]) for r in src])

# ---------- accès ----------
def authed(f):
    def w(*a, **k):
        return f(*a, **k) if session.get("ok") else redirect("/login")
    w.__name__ = f.__name__
    return w

def vue(nom, **kw):
    return render_template_string(PAGE, view=nom, chaines=liste(), cid=active(), msg=request.args.get("msg"), **kw)

@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        if request.form.get("pw") == PASSWORD:
            session["ok"] = True
            return redirect("/")
        err = "Mot de passe incorrect."
    return render_template_string(PAGE, view="login", err=err, chaines=[], cid=None, msg=None)

@app.route("/canal/<cid>")
@authed
def canal(cid):
    session["cid"] = cid
    return redirect(request.args.get("retour", "/"))

@app.route("/")
@authed
def home():
    return vue("publier")

@app.route("/connect")
@authed
def connect():
    f = flow()
    url, state = f.authorization_url(access_type="offline", prompt="consent select_account")
    session["cv"] = f.code_verifier
    return redirect(url)

@app.route("/oauth/callback")
@authed
def callback():
    f = flow()
    f.code_verifier = session.get("cv")
    f.fetch_token(authorization_response=BASE + request.full_path)
    yt = build("youtube", "v3", credentials=f.credentials)
    ch = yt.channels().list(part="snippet", mine=True).execute()["items"][0]
    d = charger(ch["id"]) or {}
    d.update(nom=ch["snippet"]["title"], token=f.credentials.to_json())
    sauver(ch["id"], d)
    session["cid"] = ch["id"]
    return redirect("/?msg=Chaîne connectée : " + ch["snippet"]["title"])

@app.route("/upload", methods=["POST"])
@authed
def upload():
    cid = active()
    if not cid: return redirect("/connect")
    yt, cfg = yt_for(cid), config(cid)
    hist = historique(yt)
    titre, tags, when, appr = choisir(hist, cfg)
    saisi = request.form.get("title", "").strip()
    inspire = None if saisi else titre_inspire(hist)
    source = ("écrit par toi" if saisi else "inspiré de tes meilleurs titres" if inspire
              else "tiré de ta liste" if titre else "IA")
    titre = saisi or inspire or titre or titre_ia("ASMR relaxant")
    texte = " ".join(tags)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    request.files["video"].save(tmp.name)
    body = {"snippet": {"title": titre[:100], "description": texte, "categoryId": "22"},
            "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                       "publishAt": when.strftime("%Y-%m-%dT%H:%M:%S.000Z")}}
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(tmp.name, chunksize=8 * 1024 * 1024, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    os.unlink(tmp.name)
    mode = f"titre {source} · hashtags " + ("optimisés" if appr else "en test")
    return redirect(f"/?msg=Programmée le {when.astimezone(TZ):%d/%m à %Hh} — « {titre} » ({mode})")

@app.route("/calendrier")
@authed
def cal():
    cid = active()
    if not cid: return redirect("/")
    m = request.args.get("m") or datetime.now(TZ).strftime("%Y-%m")
    a, mo = map(int, m.split("-"))
    jours = {}
    for v in historique(yt_for(cid)):
        jours.setdefault(v["pub"].astimezone(TZ).date(), []).append(v)
    sem = calendar.Calendar().monthdatescalendar(a, mo)
    prec = (datetime(a, mo, 1) - timedelta(days=1)).strftime("%Y-%m")
    suiv = (datetime(a, mo, 28) + timedelta(days=5)).strftime("%Y-%m")
    now = datetime.now(timezone.utc)
    mois_fr = ["Janvier","Février","Mars","Avril","Mai","Juin","Juillet","Août","Septembre","Octobre","Novembre","Décembre"]
    agenda = {d.isoformat(): [dict(t=v["titre"], h=v["pub"].astimezone(TZ).strftime("%Hh%M"),
              s="Publiée" if v["pub"] < now else "Programmée", v=v["vues"]) for v in l] for d, l in jours.items()}
    return vue("cal", sem=sem, jours=jours, mois=m, mo=mo, prec=prec, suiv=suiv, now=now,
               titre_mois=f"{mois_fr[mo-1]} {a}", today=datetime.now(TZ).date(), agenda=agenda)

@app.route("/bibliotheque")
@authed
def biblio():
    cid = active()
    if not cid: return redirect("/")
    return vue("biblio", vids=historique(yt_for(cid)), tz=TZ, now=datetime.now(timezone.utc))

@app.route("/stats")
@authed
def stats():
    cid = active()
    if not cid: return redirect("/")
    j = int(request.args.get("j", 28)); j = j if j in (7, 28, 90) else 28
    yt = yt_for(cid); v = mesurees(historique(yt))
    top = lambda cles: sorted(((k, n, round(m, 2)) for k, (n, m) in stats_par(v, cles).items()),
                              key=lambda x: -x[2])[:6]
    an, err = None, None
    try: an = analytique(cid, yt, j)
    except Exception as e: err = str(e)[:260]
    return vue("stats", j=j, an=an, err=err, nm=len(v), mini=MIN_APPRENTISSAGE,
               heures=top(lambda x: [f"{x['pub'].astimezone(TZ).hour}h"]), titres=top(lambda x: [x["titre"]]),
               tags=top(hashtags_de))

@app.route("/reglages", methods=["GET", "POST"])
@authed
def reglages():
    cid = active()
    if not cid: return redirect("/")
    if request.method == "POST":
        f = request.form
        sp = lambda s: [x.strip() for x in re.split(r"[,\n]", s) if x.strip()]
        d = charger(cid)
        d["config"] = {"titres": [x.strip() for x in f["titres"].splitlines() if x.strip()],
                       "oblig": sp(f["oblig"]) or OBLIG_TAGS, "pool": sp(f["pool"]),
                       "heures": [int(x) for x in sp(f["heures"]) if x.isdigit() and int(x) < 24] or [18],
                       "apprentissage": bool(f.get("apprentissage"))}
        sauver(cid, d)
        return redirect("/reglages?msg=Réglages enregistrés.")
    return vue("reglages", c=config(cid))

PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Programmateur YouTube</title><style>
:root{--ink:#1c1c1e;--mut:#6e6e73;--glass:rgba(255,255,255,.5);--edge:rgba(255,255,255,.8);--line:rgba(60,60,67,.16);
--acc:#0a84ff;--tint:rgba(10,132,255,.2);--pub:rgba(52,199,89,.3);--prog:rgba(255,159,10,.34);--red:#ff3b30;
--bg:linear-gradient(160deg,#d6e4ff,#f1e6ff 55%,#ffe6dc)}
@media(prefers-color-scheme:dark){:root{--ink:#f5f5f7;--mut:#a1a1a6;--glass:rgba(255,255,255,.08);--edge:rgba(255,255,255,.16);
--line:rgba(255,255,255,.12);--acc:#64b5ff;--tint:rgba(100,181,255,.22);--pub:rgba(48,209,88,.32);--prog:rgba(255,159,10,.36);
--bg:linear-gradient(160deg,#0a0f1f,#1a1033 55%,#2b1222)}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{background:var(--bg) fixed;scroll-behavior:smooth}
body{margin:0;min-height:100vh;color:var(--ink);font:16px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue",sans-serif;
padding:max(14px,env(safe-area-inset-top)) 16px max(28px,env(safe-area-inset-bottom));-webkit-font-smoothing:antialiased}
main{max-width:860px;margin:0 auto}
h1{font-size:1.9rem;font-weight:700;letter-spacing:-.02em;line-height:1.15;margin:.5em 0 .25em}
h2{font-size:1.05rem;font-weight:600;margin:1.5em 0 .5em}p{color:var(--mut);margin:.2em 0 1em}a{color:var(--acc);text-decoration:none}
label{display:block;font-weight:600;font-size:.92rem;margin:1.2em 0 .45em}
.g,input,textarea,button,.btn,nav a,.v,.msg,table,.cal{background:var(--glass);-webkit-backdrop-filter:blur(26px) saturate(1.8);backdrop-filter:blur(26px) saturate(1.8);
border:1px solid var(--edge);box-shadow:inset 0 1px 0 rgba(255,255,255,.28),0 8px 28px rgba(0,0,0,.07)}
input,textarea{width:100%;font:inherit;color:var(--ink);padding:13px 15px;border-radius:14px;outline:0;transition:box-shadow .2s,border-color .2s}
input:focus,textarea:focus{border-color:var(--acc);box-shadow:0 0 0 3px var(--tint)}textarea{min-height:90px;resize:vertical}
button,.btn{display:block;width:100%;margin-top:1.4em;padding:15px;font:inherit;font-weight:600;color:var(--acc);background:var(--tint);
border-radius:16px;text-align:center;cursor:pointer;transition:transform .18s cubic-bezier(.2,.8,.2,1),filter .2s}
button:active,.btn:active,nav a:active{transform:scale(.96)}button:disabled{opacity:.55}
nav{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}nav a{padding:8px 15px;border-radius:999px;color:var(--ink);font-size:.93rem;transition:transform .18s,background .2s}
nav a.on{background:var(--tint);color:var(--acc);font-weight:600}
.msg{padding:13px 16px;border-radius:16px;margin:1em 0}.err{color:var(--red)}
.chk{display:flex;gap:12px;align-items:center;font-weight:500}.chk input{width:24px;height:24px;padding:0}
.bar{display:none;height:8px;border-radius:99px;overflow:hidden;margin-top:1em;background:var(--line)}.bar i{display:block;height:100%;width:0;background:var(--acc);transition:width .25s}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}.v{border-radius:18px;overflow:hidden;font-size:.88rem}
.v img{width:100%;display:block}.v div{padding:10px 12px}.mut{color:var(--mut)}
table{width:100%;border-collapse:separate;border-spacing:0;border-radius:16px;overflow:hidden;font-size:.9rem}td,th{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line)}
.ch{display:flex;align-items:center;justify-content:space-between}.ch div{display:flex;gap:6px}.ch a{padding:7px 14px;border-radius:999px;background:var(--glass);border:1px solid var(--edge);font-weight:600}
.cal{border-radius:22px;overflow:hidden;margin-top:.6em}.cw,.cg{display:grid;grid-template-columns:repeat(7,1fr)}
.cw span{text-align:center;padding:9px 0;font-size:.78rem;color:var(--mut);border-bottom:1px solid var(--line)}
.d{min-height:104px;padding:5px 4px;border-right:1px solid var(--line);border-bottom:1px solid var(--line);cursor:pointer;overflow:hidden;transition:background .2s}
.d:nth-child(7n){border-right:0}.d:active,.d.sel{background:var(--tint)}.d.out{opacity:.38}
.n{display:inline-flex;width:26px;height:26px;align-items:center;justify-content:center;border-radius:50%;font-size:.9rem;font-weight:500;margin-bottom:2px}
.today .n{background:var(--red);color:#fff;font-weight:700}
.e{display:block;font-style:normal;font-size:.68rem;line-height:1.25;padding:2px 5px;margin-top:2px;border-radius:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.e.pub{background:var(--pub)}.e.prog{background:var(--prog)}.more{display:block;font-style:normal;font-size:.68rem;color:var(--mut);padding:1px 5px}
.ag{margin-top:14px;padding:6px 16px;border-radius:20px}.ag h3{margin:12px 0 4px;font-size:1rem}.ag .it{display:flex;gap:10px;padding:9px 0;border-top:1px solid var(--line);font-size:.92rem}
.ag .dot{width:4px;border-radius:4px;flex:none}.dot.pub{background:#34c759}.dot.prog{background:#ff9f0a}
.kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:.6em 0}.kpi .g{padding:14px 16px;border-radius:18px}
.kpi span{display:block;font-size:.8rem;color:var(--mut)}.kpi b{font-size:1.6rem;letter-spacing:-.02em}
.chart{border-radius:20px;padding:12px 12px 8px}.tip{font-size:.8rem;color:var(--mut);padding:4px 4px 2px;min-height:1.6em}
.box{border-radius:20px;padding:10px 14px}.row{position:relative;display:flex;justify-content:space-between;padding:8px 0;font-size:.9rem}
.row i{position:absolute;left:0;bottom:2px;height:3px;border-radius:3px;background:var(--acc);opacity:.7}.row em{font-style:normal;color:var(--mut)}
.ch a.on{background:var(--tint);color:var(--acc)}
@media(max-width:560px){.d{min-height:74px}.e{font-size:.6rem}}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head><body><main>
{% if view=='login' %}
<h1>Accès privé</h1><form method="post"><label>Mot de passe</label>
<input type="password" name="pw" autofocus><button>Entrer</button>{% if err %}<p class="err">{{err}}</p>{% endif %}</form>
{% else %}
<nav>{% for k,l,u in [('publier','Publier','/'),('cal','Calendrier','/calendrier'),('biblio','Bibliothèque','/bibliotheque'),('stats','Stats','/stats'),('reglages','Réglages','/reglages')] %}
<a class="{{'on' if view==k}}" href="{{u}}">{{l}}</a>{% endfor %}</nav>
<nav>{% for i,n in chaines %}<a class="{{'on' if i==cid}}" href="/canal/{{i}}?retour={{request.path}}">{{n}}</a>{% endfor %}<a href="/connect">+ Chaîne</a></nav>
{% if msg %}<div class="msg">{{msg}}</div>{% endif %}
{% if not cid %}<h1>Connecte ta chaîne YouTube</h1><a class="btn" href="/connect">Connecter ma chaîne</a>
{% elif view=='publier' %}
<h1>Dépose ta vidéo.</h1><p>Titre et heure sont choisis pour toi. Les 15 hashtags sont ajoutés en description.</p>
<form id="fm" method="post" action="/upload" enctype="multipart/form-data">
<label>Vidéo ou Short</label><input type="file" name="video" accept="video/*" required>
<label>Titre (vide = choisi pour toi)</label><input name="title">
<div class="bar" id="bar"><i></i></div><p id="wt"></p><button id="bt">Programmer</button></form>
<script>
fm.addEventListener('submit',function(e){e.preventDefault();var x=new XMLHttpRequest();x.open('POST','/upload');
x.upload.onprogress=function(v){if(v.lengthComputable){var p=Math.round(v.loaded/v.total*100);bar.firstChild.style.width=p+'%';wt.textContent=p<100?'Envoi '+p+' %':'Envoi vers YouTube…'}};
x.onload=function(){if(x.status>=400){wt.textContent='Erreur côté site, réessaie.';bt.disabled=false}else{location.href=x.responseURL||'/'}};
x.onerror=function(){wt.textContent='Erreur réseau, réessaie.';bt.disabled=false};
bar.style.display='block';bt.disabled=true;x.send(new FormData(fm))});
</script>
{% elif view=='cal' %}
<div class="ch"><h1>{{titre_mois}}</h1><div><a href="/calendrier?m={{prec}}">‹</a><a href="/calendrier">Aujourd’hui</a><a href="/calendrier?m={{suiv}}">›</a></div></div>
<div class="cal"><div class="cw">{% for j in ['lun.','mar.','mer.','jeu.','ven.','sam.','dim.'] %}<span>{{j}}</span>{% endfor %}</div>
<div class="cg">{% for s in sem %}{% for d in s %}<div class="d {{'out' if d.month!=mo}} {{'today' if d==today}}" data-d="{{d.isoformat()}}">
<span class="n">{{d.day}}</span>{% for v in jours.get(d,[])[:3] %}<i class="e {{'pub' if v.pub<now else 'prog'}}">{{v.titre}}</i>{% endfor %}
{% if jours.get(d,[])|length>3 %}<i class="more">+{{jours.get(d)|length-3}} de plus</i>{% endif %}</div>{% endfor %}{% endfor %}</div></div>
<div class="ag g" id="ag"></div>
<script>
var A={{agenda|tojson}};var cells=document.querySelectorAll('.d');
function show(c){cells.forEach(function(x){x.classList.remove('sel')});c.classList.add('sel');var k=c.dataset.d,l=A[k]||[],h='<h3>'+k.split('-').reverse().join('/')+'</h3>';
if(!l.length)h+='<p>Rien ce jour-là.</p>';l.forEach(function(v){var t=document.createElement('div');t.textContent=v.t;
h+='<div class="it"><span class="dot '+(v.s=='Publiée'?'pub':'prog')+'"></span><div><b>'+t.innerHTML+'</b><br><span class="mut">'+v.s+' · '+v.h+' · '+v.v+' vues</span></div></div>'});
document.getElementById('ag').innerHTML=h}
cells.forEach(function(c){c.addEventListener('click',function(){show(c)})});
var t=document.querySelector('.today')||document.querySelector('.d:not(.out)');if(t)show(t);
</script>
{% elif view=='biblio' %}
<h1>Bibliothèque</h1><div class="grid">{% for v in vids %}<div class="v"><img src="{{v.img}}" alt=""><div><b>{{v.titre}}</b><br>
<span class="mut">{{v.vues}} vues · {{v.likes}} j’aime<br>{{'Programmée' if v.pub>now else v.statut}} · {{v.pub.astimezone(tz).strftime('%d/%m %Hh')}}</span></div></div>{% endfor %}</div>
{% elif view=='stats' %}
<div class="ch"><h1>Stats</h1><div>{% for d in [7,28,90] %}<a class="{{'on' if j==d}}" href="/stats?j={{d}}">{{d}} j</a>{% endfor %}</div></div>
{% if err %}<div class="msg">Les stats YouTube détaillées ne sont pas encore autorisées.<br>
1) Dans Google Cloud, active « YouTube Analytics API ». 2) Reconnecte ta chaîne avec le bouton ci-dessous.
<a class="btn" href="/connect">Reconnecter ma chaîne</a><p style="font-size:.75rem">{{err}}</p></div>
{% elif an %}
<div class="kpi">{% for t,vl in [('Vues',an.tot.vues),('Heures vues',an.tot.heures),('Abonnés',an.tot.abos),('J’aime',an.tot.likes)] %}
<div class="g"><span>{{t}}</span><b>{{vl}}</b></div>{% endfor %}</div>
{% for k,t in [('vues','Vues par jour'),('heures','Heures de visionnage par jour'),('abos','Abonnés gagnés par jour'),('likes','J’aime par jour')] %}
<h2>{{t}}</h2><div class="g chart" id="c_{{k}}"></div>{% endfor %}
<h2>D’où viennent tes vues</h2><div class="g box">{% for nom,vl in an.src %}<div class="row"><span>{{nom}}</span><i style="width:{{(vl/an.src[0][1]*100)|round}}%"></i><em>{{vl}}</em></div>{% endfor %}</div>
<h2>Meilleures vidéos</h2><table>{% for t,vl,h in an.top %}<tr><td>{{t}}</td><td>{{vl}} vues</td><td>{{h}} h</td></tr>{% endfor %}</table>
<script>
var AN={{an|tojson}};
function chart(id,labels,vals){var el=document.getElementById('c_'+id),W=600,H=170,p=8,n=vals.length;if(!n){el.textContent='Pas encore de données.';return}
var mx=Math.max.apply(null,vals.concat([1])),pts=vals.map(function(v,i){return[p+i*(W-2*p)/Math.max(n-1,1),H-p-(Math.max(v,0)/mx)*(H-2*p-16)]});
var d=pts.map(function(q,i){return(i?'L':'M')+q[0].toFixed(1)+' '+q[1].toFixed(1)}).join('');
el.innerHTML='<svg viewBox="0 0 '+W+' '+H+'" width="100%" style="touch-action:pan-y"><defs><linearGradient id="g'+id+'" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--acc);stop-opacity:.35"/><stop offset="1" style="stop-color:var(--acc);stop-opacity:0"/></linearGradient></defs>'
+'<path d="'+d+'L'+pts[n-1][0]+' '+H+'L'+pts[0][0]+' '+H+'Z" fill="url(#g'+id+')"/><path d="'+d+'" fill="none" style="stroke:var(--acc)" stroke-width="2.5" stroke-linejoin="round"/>'
+'<line y1="0" y2="'+H+'" style="stroke:var(--mut);display:none" stroke-dasharray="3"/><circle r="5" style="fill:var(--acc);display:none"/></svg><div class="tip">'+labels[0]+' → '+labels[n-1]+' · appuie sur le graphique</div>';
var svg=el.querySelector('svg'),tip=el.querySelector('.tip'),ln=svg.querySelector('line'),dt=svg.querySelector('circle');
function mv(e){var r=svg.getBoundingClientRect(),x=(e.clientX-r.left)/r.width*W,i=Math.max(0,Math.min(n-1,Math.round((x-p)/((W-2*p)/Math.max(n-1,1)))));
ln.setAttribute('x1',pts[i][0]);ln.setAttribute('x2',pts[i][0]);ln.style.display='';dt.setAttribute('cx',pts[i][0]);dt.setAttribute('cy',pts[i][1]);dt.style.display='';tip.textContent=labels[i]+' : '+vals[i]}
svg.addEventListener('pointermove',mv);svg.addEventListener('pointerdown',mv)}
['vues','heures','abos','likes'].forEach(function(k){chart(k,AN.labels,AN[k])});
</script>
{% endif %}
<h2>Ce que le site a appris</h2><p>{{nm}} vidéos mesurées sur {{mini}} nécessaires. {{'Actif : titres, hashtags et heures s’adaptent à ce qui marche.' if nm>=mini else 'Phase de test : il essaie des variantes pour voir ce qui marche.'}}</p>
{% for nom,l in [('Meilleures heures',heures),('Meilleurs titres',titres),('Meilleurs hashtags',tags)] %}<h2>{{nom}}</h2>
<table><tr><th>Valeur</th><th>Vidéos</th><th>Score</th></tr>{% for k,n2,m in l %}<tr><td>{{k}}</td><td>{{n2}}</td><td>{{m}}</td></tr>{% endfor %}</table>{% endfor %}
{% elif view=='reglages' %}
<h1>Réglages</h1><form method="post">
<label>Tes titres (un par ligne)</label><textarea name="titres">{{c.titres|join('\n')}}</textarea>
<label>Hashtags obligatoires (toujours en description)</label><input name="oblig" value="{{c.oblig|join(', ')}}">
<label>Hashtags candidats en plus (le site choisit les meilleurs parmi eux, 15 par vidéo)</label><textarea name="pool">{{c.pool|join(', ')}}</textarea>
<label>Heures de publication possibles (ex : 12, 18, 20)</label><input name="heures" value="{{c.heures|join(', ')}}">
<label class="chk"><input type="checkbox" name="apprentissage" {{'checked' if c.apprentissage}}> Apprendre de mes résultats</label>
<button>Enregistrer</button></form>
{% endif %}{% endif %}
</main></body></html>"""
