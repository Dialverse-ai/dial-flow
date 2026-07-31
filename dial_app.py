"""Dial Flow — Arabic+English dictation with a GPU-composited web UI.

Architecture: Python engine (recording, Cohere transcription, Flow cleanup,
hotkeys, tray, chimes) + pywebview/WebView2 frontend (web/index.html) for
true 60fps animation. Config lives in %APPDATA%\\ArabicDictation (frozen)
or the project dir (dev). Errors go to app.log.
"""

import base64
import ctypes
import io
import json
import logging
import os
import queue
import re
import sys
import threading
import shutil
import time
import wave
import webbrowser
import winreg
import winsound
from datetime import datetime

import keyboard
import numpy as np
import pyperclip
import pystray
import requests
import sounddevice as sd
import soundfile as sf
import webview
from dotenv import load_dotenv
from PIL import Image

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    RESOURCE_DIR = sys._MEIPASS
    CONFIG_DIR = os.path.join(os.environ.get("APPDATA", APP_DIR), "DialFlow")
else:
    RESOURCE_DIR = APP_DIR
    CONFIG_DIR = APP_DIR
os.makedirs(CONFIG_DIR, exist_ok=True)
# one-time migration from the Yalla Flow era: carry over key, settings and
# history so nobody re-onboards after the rename
_LEGACY_DIR = os.path.join(os.environ.get("APPDATA", ""), "ArabicDictation")
if (getattr(sys, "frozen", False)
        and not os.path.exists(os.path.join(CONFIG_DIR, ".env"))
        and os.path.isdir(_LEGACY_DIR)):
    for _f in (".env", "settings.json", "history.json"):
        _src = os.path.join(_LEGACY_DIR, _f)
        if os.path.exists(_src):
            try:
                shutil.copyfile(_src, os.path.join(CONFIG_DIR, _f))
            except OSError:
                pass

APP_VERSION = "3.2.0"
PILL_W, PILL_H = 150, 38    # expanded (recording/processing)
MINI_W, MINI_H = 36, 12     # idle bubble (the size Mike approved)
PILL_BG = "#171320"  # warm plum-black — matches the app's dark identity
UPDATE_API = "https://api.github.com/repos/Dialverse-ai/dial-flow/releases/latest"
UPDATE_EVERY_H = 6  # re-check interval; a launch-only check never reaches a
                    # tray-resident app that stays open for days
API_URL = "https://api.cohere.com/v2/audio/transcriptions"
CHAT_URL = "https://api.cohere.com/v2/chat"
MODELS_URL = "https://api.cohere.com/v1/models"
MODEL = "cohere-transcribe-arabic-07-2026"
CLEANUP_MODELS = ["command-a-03-2025", "command-r-plus-08-2024", "command-r-08-2024"]
SAMPLE_RATE = 16000
MAX_SECONDS = 300
ENV_FILE = os.path.join(CONFIG_DIR, ".env")
ICON_FILE = os.path.join(RESOURCE_DIR, "app.ico")
UI_FILE = os.path.join(RESOURCE_DIR, "web", "index.html")
HISTORY_FILE = os.path.join(CONFIG_DIR, "history.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
AUDIO_DIR = os.path.join(CONFIG_DIR, "audio")
AUDIO_KEEP = 40  # rolling cap of kept recordings (~10MB worst case)

logging.basicConfig(filename=os.path.join(CONFIG_DIR, "app.log"), level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

DEFAULT_SETTINGS = {
    "flow_mode": True,
    "rec_mode": "toggle",
    "record_key": "f9",
    "lang_key": "f10",
    "command_key": "f8",
    "chime_on": True,
    "chime_volume": 40,
    "mic_device": "",
    "dictionary": "dialverse = Dialverse",
    "language": "auto",
    "tone": "auto",
    "app_aware": True,
    "paste_mode": "paste",
    "snippets": [],
    "theme": "system",
    "autostart": False,
    "idle_pill": True,
    "audio_enhance": True,
}

# ---- per-app formatting context (Wispr-style) ----

_CTX_BY_EXE = {
    "olk.exe": "email", "outlook.exe": "email", "thunderbird.exe": "email",
    "whatsapp.exe": "chat", "slack.exe": "chat", "telegram.exe": "chat",
    "discord.exe": "chat", "ms-teams.exe": "chat", "teams.exe": "chat",
    "messenger.exe": "chat",
    "winword.exe": "docs", "notepad.exe": "docs", "notion.exe": "docs",
    "obsidian.exe": "docs", "onenote.exe": "docs", "wordpad.exe": "docs",
    "code.exe": "code", "cursor.exe": "code", "devenv.exe": "code",
    "windowsterminal.exe": "code", "wt.exe": "code",
}
# browsers get categorized by tab title instead
_CTX_TITLE_HINTS = (
    ("gmail", "email"), ("outlook", "email"), ("proton mail", "email"),
    ("whatsapp", "chat"), ("slack", "chat"), ("telegram", "chat"),
    ("discord", "chat"), ("teams", "chat"),
    ("google docs", "docs"), ("notion", "docs"),
)
APP_PROMPTS = {
    "email": "The text will be pasted into an EMAIL. Organize it as clean "
             "email prose: short clear paragraphs, complete sentences; keep a "
             "greeting or sign-off only if the speaker actually said one.",
    "chat": "The text will be pasted into a CHAT app. Keep it short and "
            "natural, like a message to a colleague — no formal restructuring.",
    "docs": "The text will be pasted into a DOCUMENT. Use structured prose "
            "with paragraph breaks; if the speaker enumerates items, format "
            "them as a list with '- ' bullets.",
    "code": "The text will be pasted into a CODE EDITOR or terminal. Plain "
            "text only: no markdown, no smart quotes, and never reformat "
            "technical tokens, paths or identifiers.",
    "general": "",
}
TONE_PROMPTS = {
    "auto": "Match the speaker's natural tone.",
    "professional": "Polish the wording into professional, "
                    "workplace-appropriate language (same meaning, no new "
                    "content).",
    "casual": "Keep the wording relaxed and conversational.",
}


def _app_context():
    """Category of the app the user is dictating into ('email', 'chat',
    'docs', 'code' or 'general'), from the foreground window's exe + title.
    Captured at record start — that's the window the paste will land in."""
    try:
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        hwnd = u32.GetForegroundWindow()
        tbuf = ctypes.create_unicode_buffer(256)
        u32.GetWindowTextW(hwnd, tbuf, 256)
        title = tbuf.value.lower()
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = ""
        h = k32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFO
        if h:
            size = ctypes.c_ulong(512)
            pbuf = ctypes.create_unicode_buffer(512)
            if k32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
                exe = os.path.basename(pbuf.value).lower()
            k32.CloseHandle(h)
        if exe in _CTX_BY_EXE:
            return _CTX_BY_EXE[exe]
        for hint, cat in _CTX_TITLE_HINTS:
            if hint in title:
                return cat
        return "general"
    except Exception:
        return "general"


def _speech_present(audio):
    """Cheap voice-activity check: is there ANY 100ms window loud enough to
    plausibly be speech? Silent accidental taps must never reach the API —
    the model hallucinates words from amplified nothing."""
    win = SAMPLE_RATE // 10
    n = len(audio) // win
    if n == 0:
        return False
    return any(
        float(np.sqrt(np.mean(audio[i * win:(i + 1) * win] ** 2))) > 0.008
        for i in range(n))


def _enhance_audio(audio):
    """Near-zero-latency mic cleanup before transcription: high-pass out
    sub-75Hz rumble, then a GENTLE lift for quiet speech. Conservative on
    purpose — hot gain amplifies background media/noise into hallucination
    fuel and pushes real speech toward clipping."""
    if len(audio) < 1600:
        return audio
    spec = np.fft.rfft(audio)
    freqs = np.fft.rfftfreq(len(audio), 1.0 / SAMPLE_RATE)
    spec[freqs < 75] = 0
    audio = np.fft.irfft(spec, n=len(audio)).astype(np.float32)
    rms = float(np.sqrt(np.mean(audio ** 2)))
    if 0.005 < rms < 0.06:
        # only lift genuinely-quiet speech, mildly (max 12 dB), and never
        # touch clips that are near-silence (nothing there to lift)
        audio = audio * min(0.06 / rms, 4.0)
    peak = float(np.max(np.abs(audio)))
    if peak > 0.90:
        audio = audio * (0.90 / peak)
    return audio


def _apply_autostart(enabled):
    """Register/unregister launch-at-login via the per-user Run key —
    the no-installer way to start with Windows. Only meaningful when frozen."""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        if enabled and getattr(sys, "frozen", False):
            winreg.SetValueEx(key, "DialFlow", 0, winreg.REG_SZ,
                              f'"{sys.executable}" --minimized')
        else:
            try:
                winreg.DeleteValue(key, "DialFlow")
            except FileNotFoundError:
                pass
        try:  # drop the Yalla Flow era's entry so both don't launch
            winreg.DeleteValue(key, "YallaFlow")
        except FileNotFoundError:
            pass
        winreg.CloseKey(key)
    except OSError:
        logging.exception("autostart update failed")

PILL_HTML = """<!DOCTYPE html><html><head><style>
*{margin:0;padding:0}html,body{background:#171320;overflow:hidden}
body{font-family:'Segoe UI',sans-serif;height:100vh;box-sizing:border-box;
position:relative;border:1px solid rgba(255,248,235,.26);border-radius:8px}
body.mini{border:none;border-radius:4px;
box-shadow:inset 0 0 0 1px rgba(255,248,235,.16)}
/* design 2a keyframes — authoritative */
@keyframes pillBreathe{0%,100%{opacity:.3;transform:scaleX(1)}50%{opacity:.75;transform:scaleX(1.12)}}
@keyframes coreIn{from{opacity:0;transform:scale(.6)}to{opacity:1;transform:scale(1)}}
@keyframes pillDot{0%,100%{transform:scale(1);opacity:1}50%{transform:scale(.72);opacity:.55}}
@keyframes pillShim{0%,100%{opacity:.18}50%{opacity:.9}}
@keyframes pillSpin{to{transform:rotate(360deg)}}
@keyframes pillPop{from{opacity:0;transform:scale(.6)}to{opacity:1;transform:scale(1)}}
@keyframes pillRise{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
@keyframes pillFlash{0%,46%{opacity:1}100%{opacity:0}}
/* layers */
.layer{position:absolute;inset:0;display:flex;align-items:center;
justify-content:center;gap:8px}
#core{width:24px;height:6px;border-radius:3px;
background:radial-gradient(closest-side,rgba(180,156,250,.95),rgba(108,85,232,.4) 65%,rgba(108,85,232,0));
animation:coreIn .4s ease-out both,pillBreathe 3.6s ease-in-out .4s infinite}
#recwash{position:absolute;inset:0;border-radius:7px;
box-shadow:inset 0 0 12px rgba(242,107,94,.18)}
#flashrim{position:absolute;inset:0;border-radius:7px;
box-shadow:inset 0 0 10px rgba(212,198,255,.45);animation:pillFlash .3s ease-out both}
.dotwrap{display:flex;animation:pillPop .12s ease-out .08s both}
.dotwrap i{width:9px;height:9px;border-radius:999px;background:#F26B5E;display:block;
box-shadow:0 0 8px rgba(242,107,94,.5);
animation:pillDot 1.4s ease-in-out .2s infinite}
#t{font-size:13px;font-weight:600;color:#F7F4EC;font-variant-numeric:tabular-nums;
animation:pillRise .14s ease-out .12s both}
#wave{display:flex;align-items:center;gap:2px;animation:pillRise .16s ease-out .16s both}
#wave i{width:2px;height:2px;border-radius:1px;background:#FF8B7C;display:block}
.pspin{display:flex;animation:pillPop .12s ease-out .06s both}
.pspin i{width:13px;height:13px;border-radius:999px;display:block;
border:2px solid rgba(156,134,246,.25);border-top-color:#9C86F6;
animation:pillSpin .8s linear infinite}
#pdots{display:flex;align-items:center;gap:3.5px;animation:pillRise .16s ease-out .1s both}
#pdots i{width:2.5px;height:2.5px;border-radius:999px;background:#8F86B8;display:block;
animation:pillShim 1.32s ease-in-out infinite}
/* state visibility */
.layer,#recwash,#flashrim{display:none}
body.mini #core-l{display:flex}
body.rec #rec-l{display:flex}
body.rec #recwash{display:block}
body.processing #proc-l{display:flex}
body.flash #proc-l{display:flex}
body.flash #flashrim{display:block}
body.flash .pspin{display:none}
body.flash #pdots{animation:none}
body.flash #pdots i{background:#C9BEFF;animation:pillFlash .3s ease-out both}
</style></head><body class="mini">
<div id="recwash"></div><div id="flashrim"></div>
<div class="layer" id="core-l"><div id="core"></div></div>
<div class="layer" id="rec-l">
 <span class="dotwrap"><i></i></span>
 <div id="t">0:00</div>
 <div id="wave"></div>
</div>
<div class="layer" id="proc-l" style="gap:9px">
 <span class="pspin"><i></i></span>
 <div id="pdots"></div>
</div>
<script>
let startedAt=0,lvl=0,disp=0;
const H=[8,14,20,11,17,22,9,15,12,18,10,16];
const wave=document.getElementById('wave');
const bars=H.map(()=>{const b=document.createElement('i');
wave.appendChild(b);return b;});
const pd=document.getElementById('pdots');
for(let i=0;i<12;i++){const d=document.createElement('i');
d.style.animationDelay=(i*0.11)+'s';pd.appendChild(d);}
window.app={
 start(ts){startedAt=ts;lvl=0;disp=0;document.body.className='rec';},
 level(v){lvl=v;},
 mode(m){document.body.className=m;},
 done(){document.body.className='processing flash';}
};
function raf(){
 if(document.body.classList.contains('rec')){
  if(startedAt){
   const s=Math.max(0,Math.floor(Date.now()/1000-startedAt));
   document.getElementById('t').textContent=
    Math.floor(s/60)+':'+String(s%60).padStart(2,'0');
  }
  /* Wispr-style wave: bars are driven by the real mic level pushed from
     Python — silence collapses them to a flat 2px line, speech lifts them
     with a slight per-bar wobble (wobble scales with the level too, so a
     quiet room never shimmies) */
  const target=Math.min(1,lvl*9);
  disp+=(target-disp)*(target>disp?0.4:0.15);
  const t=performance.now()/1000;
  bars.forEach((b,i)=>{
   const wig=0.7+0.3*Math.sin(t*9+i*1.7);
   b.style.height=(2+disp*(H[i]-2)*wig)+'px';
  });
 }
 requestAnimationFrame(raf);}
requestAnimationFrame(raf);
</script></body></html>"""


class Settings(dict):
    def __init__(self):
        super().__init__(DEFAULT_SETTINGS)
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                self.update(json.load(f))
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(dict(self), f, ensure_ascii=False, indent=1)
        except OSError:
            logging.exception("settings save failed")


def _bar_note(freq, dur, sr, decay=16.0):
    """One soft struck-bar (marimba-like) note: warm detuned harmonics with a
    fast attack and natural exponential decay — not a raw electronic sine."""
    n = int(sr * dur)
    t = np.arange(n) / sr
    env = np.exp(-t * decay)
    attack = min(int(sr * 0.004), n)
    env[:attack] *= np.linspace(0.0, 1.0, attack)
    w = (np.sin(2 * np.pi * freq * t)
         + 0.34 * np.sin(2 * np.pi * freq * 2.008 * t)
         + 0.13 * np.sin(2 * np.pi * freq * 3.011 * t))
    return w * env


def _chime_wav(notes, amp, sr=44100, total=0.45, decay=16.0):
    """notes: (freq_hz, offset_ms) pairs layered into one soft chime."""
    total_len = int(sr * total)
    mix = np.zeros(total_len)
    for freq, off_ms in notes:
        note = _bar_note(freq, max(0.05, total - 0.02), sr, decay)
        i0 = int(sr * off_ms / 1000)
        end = min(total_len, i0 + len(note))
        mix[i0:end] += note[:end - i0]
    peak = np.max(np.abs(mix))
    if peak > 0:
        mix *= amp / peak
    pcm = (mix * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(pcm.tobytes())
    return buf.getvalue()


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("padding", ctypes.c_byte * 32)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUT_UNION)]


_paste_extra = ctypes.c_ulong(0)


def _send_ctrl_vk(letter_vk):
    """Layout-independent Ctrl+<key> via SendInput virtual keys.

    keyboard.send('ctrl+v') resolves the letter through the ACTIVE keyboard
    layout — with an Arabic layout (browsers, WhatsApp Web) that misfires and
    nothing happens. VK codes name the physical shortcut, which Windows apps
    recognize under any layout."""
    def key(vk, up=False):
        inp = _INPUT(type=1)  # INPUT_KEYBOARD
        inp.ki = _KEYBDINPUT(vk, 0, 2 if up else 0, 0,
                             ctypes.pointer(_paste_extra))
        return inp

    seq = [key(0x11), key(letter_vk), key(letter_vk, True), key(0x11, True)]
    arr = (_INPUT * len(seq))(*seq)
    sent = ctypes.windll.user32.SendInput(len(seq), arr, ctypes.sizeof(_INPUT))
    if sent != len(seq):
        logging.error("key injection incomplete: %s/%s events", sent, len(seq))


def _send_paste():
    _send_ctrl_vk(0x56)  # Ctrl+V


def _send_copy():
    _send_ctrl_vk(0x43)  # Ctrl+C


def _send_type(text):
    """Simulate real keystrokes via KEYEVENTF_UNICODE for fields that block
    Ctrl+V (terminals, some CRM iframes). Newlines go as VK_RETURN — many
    editors ignore a typed U+000A. Sent in chunks so target apps keep up."""
    UNICODE, KEYUP = 0x0004, 0x0002

    def ki(vk, scan, flags):
        inp = _INPUT(type=1)
        inp.ki = _KEYBDINPUT(vk, scan, flags, 0, ctypes.pointer(_paste_extra))
        return inp

    seq = []
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            seq += [ki(0x0D, 0, 0), ki(0x0D, 0, KEYUP)]
            continue
        units = ch.encode("utf-16-le")
        for i in range(0, len(units), 2):
            scan = units[i] | (units[i + 1] << 8)
            seq += [ki(0, scan, UNICODE), ki(0, scan, UNICODE | KEYUP)]
    for i in range(0, len(seq), 200):
        chunk = seq[i:i + 200]
        arr = (_INPUT * len(chunk))(*chunk)
        ctypes.windll.user32.SendInput(len(chunk), arr, ctypes.sizeof(_INPUT))
        time.sleep(0.01)


def _parse_dictionary(text):
    pairs = []
    for line in text.splitlines():
        if "=" in line:
            a, b = line.split("=", 1)
        elif "->" in line:
            a, b = line.split("->", 1)
        else:
            continue
        a, b = a.strip(), b.strip()
        if a and b:
            pairs.append((a, b))
    return pairs


def _apply_dictionary(text, pairs):
    for a, b in pairs:
        if a.isascii():
            text = re.sub(rf"\b{re.escape(a)}\b", b, text, flags=re.IGNORECASE)
        else:
            text = text.replace(a, b)
    return text


def _match_snippet(text, snippets):
    """Say just a snippet's trigger, get its template pasted. Exact match,
    forgiving about case, whitespace and trailing punctuation."""
    norm = re.sub(r"\s+", " ", text).strip().strip(".!?,،؟ ").lower()
    if not norm:
        return None
    for s in snippets or []:
        trig = (s.get("t") or "").strip().lower()
        if trig and norm == trig:
            return s.get("x") or None
    return None


class Engine:
    """Recording + transcription + optional AI cleanup. UI-agnostic."""

    def __init__(self, api_key, settings, on_state, on_transcript, on_language):
        self.api_key = api_key
        self.settings = settings
        self.on_state = on_state
        self.on_transcript = on_transcript
        self.on_language = on_language
        self.language = settings.get("language", "auto")
        self.recording = False
        self.frames = queue.Queue()
        self.stream = None
        self.started_at = None
        self.level = 0.0
        self.lock = threading.Lock()
        self._clean_model_idx = 0
        self.mode = "dictate"       # or "command" (voice-edit selection)
        self._app_ctx = "general"   # focused-app category at record start
        self._cmd_selection = ""
        # serializes the clipboard between command-mode's empty-sentinel
        # capture and worker threads pasting results
        self._clip_lock = threading.Lock()
        self.rebuild_chimes()
        # first device-open of a session is the slow one — take that hit now
        threading.Thread(target=self._prewarm_mic, daemon=True).start()

    def _prewarm_mic(self):
        try:
            s = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                               dtype="float32", device=self._resolve_device())
            s.start()
            s.stop()
            s.close()
        except Exception:
            pass

    def rebuild_chimes(self):
        amp = max(0, min(100, self.settings.get("chime_volume", 40))) / 100 * 0.5
        # start: short ~140ms tick — it plays synchronously before the mic
        # opens, so its length is pure key-to-recording latency
        self.start_wav = _chime_wav([(659, 0), (988, 55)], amp,
                                    total=0.14, decay=30.0)
        # stop: full marimba tail (plays async, costs nothing)
        self.stop_wav = _chime_wav([(880, 0), (587, 65)], amp)

    def _play(self, wav_bytes, block):
        if not self.settings.get("chime_on", True):
            return
        if block:
            winsound.PlaySound(wav_bytes, winsound.SND_MEMORY)
        else:
            threading.Thread(target=winsound.PlaySound,
                             args=(wav_bytes, winsound.SND_MEMORY),
                             daemon=True).start()

    def _audio_callback(self, indata, frames_count, time_info, status):
        if self.recording:
            self.frames.put(indata.copy())
            self.level = float(np.sqrt(np.mean(indata ** 2)))

    def _resolve_device(self):
        name = self.settings.get("mic_device", "")
        if not name:
            return None
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0 and dev["name"] == name:
                return i
        return None

    def toggle_language(self):
        order = ["auto", "ar", "en"]
        cur = self.language if self.language in order else "auto"
        self.language = order[(order.index(cur) + 1) % len(order)]
        self.settings["language"] = self.language
        self.settings.save()
        self.on_language(self.language)

    def start_recording(self):
        try:
            self._start_inner()
        except Exception as e:
            logging.exception("start_recording failed")
            self.recording = False
            self.on_state("error", f"Error: {e}")

    def stop_recording(self):
        try:
            self._stop_inner()
        except Exception as e:
            logging.exception("stop_recording failed")
            self.recording = False
            self.on_state("error", f"Error: {e}")

    def toggle_recording(self):
        if self.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def _start_inner(self):
        with self.lock:
            if self.recording:
                return
            self._starting = True
            # NB: _want_stop is NOT reset here — hold-to-talk can release the
            # key during start_command's clipboard capture (before this runs),
            # and that early release must still stop the recording below
            # UI first: the pill starts expanding the instant the key lands;
            # the chime and mic-open happen behind the animation
            self.started_at = time.time()
            if self.mode != "command" and self.settings.get("app_aware", True):
                self._app_ctx = _app_context()
            else:
                self._app_ctx = "general"
            self.on_state("recording",
                          "command" if self.mode == "command" else "")
            self._play(self.start_wav, block=True)  # ~140ms, before mic opens
            self.frames = queue.Queue()
            try:
                self.stream = sd.InputStream(
                    samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                    device=self._resolve_device(),
                    callback=self._audio_callback,
                )
                self.stream.start()
            except Exception as e:
                logging.exception("mic open failed")
                self._starting = False
                self._want_stop = False
                self.mode = "dictate"
                self.on_state("error", f"Mic error: {e}")
                return
            self.recording = True
            self._starting = False
        # hold-to-talk: if the key was released during the ~300ms prep,
        # honor it now instead of recording forever
        if getattr(self, "_want_stop", False):
            self._want_stop = False
            self._stop_inner()

    def _stop_inner(self):
        with self.lock:
            if not self.recording:
                if getattr(self, "_starting", False):
                    self._want_stop = True
                return
            self.recording = False
            self.level = 0.0
            self.stream.stop()
            self.stream.close()
            self._play(self.stop_wav, block=False)
            elapsed = time.time() - self.started_at
            chunks = []
            while not self.frames.empty():
                chunks.append(self.frames.get())
            mode, self.mode = self.mode, "dictate"
            if not chunks or elapsed < 0.3:
                self.on_state("idle", "Too short — ignored")
                return
            audio = np.concatenate(chunks).flatten()[: MAX_SECONDS * SAMPLE_RATE]
            if not _speech_present(audio):
                # silent/accidental tap — never send it: the model invents
                # words ("فراغ", "♫") from amplified nothing
                self.on_state("idle", "Nothing heard — skipped")
                return
            if self.settings.get("audio_enhance", True):
                try:
                    audio = _enhance_audio(audio)
                except Exception:
                    logging.exception("audio enhance failed — using raw")
            self.on_state("transcribing", "")
            if mode == "command":
                # snapshot the selection NOW — a second command-mode session
                # started mid-transcription must not swap it under this worker
                threading.Thread(
                    target=self._command_transcribe,
                    args=(audio, elapsed, self.language, self._cmd_selection),
                    daemon=True).start()
            else:
                threading.Thread(
                    target=self._transcribe,
                    args=(audio, elapsed, self.language), daemon=True).start()

    def _asr(self, wav_bytes, lang):
        """One transcription call. Returns (text, error) — exactly one is set.

        'auto' maps straight to 'ar': the API REQUIRES a language and rejects
        'auto' (probed 2026-07-31), and the code-switch model transcribes
        pure English fine under 'ar' — so auto costs zero extra uploads.

        Transient failures (flaky uplink kills big WAV uploads mid-write,
        seen in the field) retry up to 3 attempts with backoff; the (10,120)
        timeout gives slow uploads room the old flat 60s didn't."""
        data = {"model": MODEL, "language": "ar" if lang == "auto" else lang}
        last_err = "Network error — check connection"
        for attempt in range(3):
            try:
                resp = requests.post(
                    API_URL,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    data=data,
                    files={"file": ("audio.wav", io.BytesIO(wav_bytes),
                                    "audio/wav")},
                    timeout=(10, 120),
                )
            except requests.RequestException:
                logging.exception("asr attempt %s/3 failed", attempt + 1)
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 200:
                return resp.json().get("text", "").strip(), None
            if resp.status_code == 429:
                last_err = "Rate limited — try again in a minute"
                time.sleep(3.0)
                continue
            logging.error("api %s: %s", resp.status_code, resp.text[:300])
            return None, f"API error {resp.status_code}"
        return None, last_err

    def _keep_audio(self, wav_bytes, t0):
        """Persist the take BEFORE any network I/O — a failed upload must
        never cost the user a re-record. Rolling cap; oldest pruned."""
        try:
            os.makedirs(AUDIO_DIR, exist_ok=True)
            name = f"{int(t0 * 1000)}.wav"
            with open(os.path.join(AUDIO_DIR, name), "wb") as f:
                f.write(wav_bytes)
            for old in sorted(os.listdir(AUDIO_DIR))[:-AUDIO_KEEP]:
                os.remove(os.path.join(AUDIO_DIR, old))
            return name
        except OSError:
            logging.exception("audio save failed")
            return ""

    def _worker_state(self, state, detail=""):
        """State pushes from finished/finishing workers. A live recording
        owns the status UI — a stale worker's 'cleaning'/'idle'/'error' must
        not stomp it on the main window or the pill."""
        if self.recording:
            return
        self.on_state(state, detail)

    def _insert_text(self, text):
        """Deliver text into the focused field. The transcript always lands
        in the clipboard too — manual Ctrl+V is the recovery path."""
        with self._clip_lock:
            pyperclip.copy(text)
            if self.settings.get("paste_mode", "paste") == "type":
                _send_type(text)
            else:
                time.sleep(0.15)  # let the clipboard write commit first
                _send_paste()

    def _transcribe(self, audio, duration, lang):
        t0 = time.time()
        buf = io.BytesIO()
        sf.write(buf, audio, SAMPLE_RATE, format="WAV", subtype="PCM_16")
        wav = buf.getvalue()
        audio_name = self._keep_audio(wav, t0)
        text, err = self._asr(wav, lang)
        if err:
            # the take is safe on disk — surface a retryable failed entry
            # in the feed instead of throwing the recording away
            entry = {
                "ts": time.time(), "lang": lang, "text": "",
                "secs": round(duration, 1), "words": 0,
                "latency": round(time.time() - t0, 1), "cleaned": False,
                "raw": "", "audio": audio_name, "app": self._app_ctx,
                "failed": err,
            }
            self.on_transcript(entry)
            self._worker_state("error", err + (" — take saved, retry from "
                                               "history" if audio_name else ""))
            return
        if not text:
            self._worker_state("idle", "Nothing recognized")
            return

        snippet = _match_snippet(text, self.settings.get("snippets"))
        if snippet is not None:
            self._insert_text(snippet)
            entry = {
                "ts": time.time(), "lang": lang, "text": snippet,
                "secs": round(duration, 1), "words": len(snippet.split()),
                "latency": round(time.time() - t0, 1), "cleaned": False,
                "raw": "", "audio": audio_name, "app": self._app_ctx,
                "snippet": True,
            }
            self.on_transcript(entry)
            self._worker_state("idle", "")
            return

        raw_text = text
        cleaned = False
        if self.settings.get("flow_mode", True):
            self._worker_state("cleaning", "")
            out = self._flow_clean(text, self._app_ctx)
            if out:
                text, cleaned = out, True
            else:
                logging.warning("flow cleanup unavailable — pasted raw")

        text = _apply_dictionary(
            text, _parse_dictionary(self.settings.get("dictionary", "")))

        self._insert_text(text)
        entry = {
            "ts": time.time(),
            "lang": lang,
            "text": text,
            "secs": round(duration, 1),
            "words": len(text.split()),
            "latency": round(time.time() - t0, 1),
            "cleaned": cleaned,
            "raw": raw_text if cleaned else "",  # for "Undo AI edit"
            "audio": audio_name,                 # for retry / extract audio
            "app": self._app_ctx,                # for per-app insights
        }
        self.on_transcript(entry)
        self._worker_state("idle", "" if cleaned or not self.settings.get(
            "flow_mode", True) else "Pasted raw — Flow couldn't reach the AI")

    def transcribe_file(self, path, lang):
        """Re-run transcription on a kept recording. Returns text or None."""
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            logging.exception("retry: audio read failed")
            return None
        text, err = self._asr(data, lang)
        if err:
            logging.error("retry failed: %s", err)
        return text or None

    # ---------- command mode (voice-edit selection) ----------

    def start_command(self):
        """Wispr-style voice edit: grab the current selection via clipboard,
        then record a spoken instruction to apply to it."""
        if self.recording:
            return
        # mark "starting" through the clipboard capture too, so a hold-mode
        # release in this window queues _want_stop instead of getting lost
        self._starting = True
        with self._clip_lock:
            try:
                old_clip = pyperclip.paste()
            except Exception:
                old_clip = ""
            try:
                pyperclip.copy("")   # sentinel: empty = nothing was selected
                _send_copy()
                time.sleep(0.18)     # let the target app service WM_COPY
                sel = pyperclip.paste()
            except Exception:
                logging.exception("command: clipboard capture failed")
                sel = ""
        if not sel.strip():
            self._starting = False
            self._want_stop = False
            try:
                pyperclip.copy(old_clip)
            except Exception:
                pass
            self.on_state("error", "Select some text first, then press "
                          f"{self.settings.get('command_key', 'f8').upper()}")
            return
        self._cmd_selection = sel
        self.mode = "command"
        self.start_recording()

    def toggle_command(self):
        if self.recording:
            self.stop_recording()
        else:
            self.start_command()

    def stop_command(self):
        self.stop_recording()

    def _command_transcribe(self, audio, duration, lang, selection):
        t0 = time.time()
        buf = io.BytesIO()
        sf.write(buf, audio, SAMPLE_RATE, format="WAV", subtype="PCM_16")
        instruction, err = self._asr(buf.getvalue(), "auto")
        if err or not instruction:
            self._worker_state("error", err or "Didn't catch the instruction")
            return
        self._worker_state("cleaning", "")
        out = self._chat(
            "You edit text by voice command. Apply the INSTRUCTION to the "
            "TEXT and return ONLY the edited text — no quotes, no commentary, "
            "no explanation. Preserve the text's original language and "
            "formatting style unless the instruction says otherwise (e.g. "
            "asks for a translation).\n\n"
            f"INSTRUCTION: {instruction}\n\nTEXT:\n{selection}")
        if not out:
            self._worker_state("error", "Edit failed — try again")
            return
        # insertion replaces the still-highlighted selection in the target app
        self._insert_text(out)
        entry = {
            "ts": time.time(),
            "lang": lang,
            "text": out,
            "secs": round(duration, 1),
            "words": len(out.split()),
            "latency": round(time.time() - t0, 1),
            "cleaned": True,
            "raw": selection,     # "Undo AI edit" restores the original
            "cmd": instruction,   # shown as the edit badge tooltip
            "audio": "",
        }
        self.on_transcript(entry)
        self._worker_state("idle", "")

    # ---------- AI cleanup ----------

    def _chat(self, prompt):
        """One Cohere chat call with the model-fallback chain. Transient
        failures (429, network blips) retry — a silently skipped cleanup
        reads to the user as 'the feature is broken'."""
        transient = 0
        while self._clean_model_idx < len(CLEANUP_MODELS):
            model = CLEANUP_MODELS[self._clean_model_idx]
            try:
                resp = requests.post(
                    CHAT_URL,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": model,
                          "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0.1},
                    timeout=(10, 45),
                )
            except requests.RequestException:
                logging.exception("chat call failed")
                transient += 1
                if transient > 2:
                    return None
                time.sleep(1.5 * transient)
                continue
            if resp.status_code == 200:
                try:
                    parts = resp.json()["message"]["content"]
                    out = "".join(p.get("text", "") for p in parts).strip()
                    return out or None
                except (KeyError, IndexError, TypeError):
                    logging.error("chat parse failed: %s", resp.text[:300])
                    return None
            if resp.status_code == 429:
                transient += 1
                if transient > 2:
                    return None
                time.sleep(3.0)
                continue
            if resp.status_code in (400, 404):
                logging.warning("chat model %s unavailable (%s)", model,
                                resp.status_code)
                self._clean_model_idx += 1
                continue
            logging.warning("chat api %s: %s", resp.status_code, resp.text[:200])
            return None
        # every model 404'd (key lost access?) — re-probe from the top next
        # call instead of staying dead for the rest of the session
        self._clean_model_idx = 0
        return None

    def _flow_clean(self, text, app_ctx="general"):
        tone = TONE_PROMPTS.get(self.settings.get("tone", "auto"),
                                TONE_PROMPTS["auto"])
        ctx = APP_PROMPTS.get(app_ctx, "")
        prompt = (
            "You are a dictation post-processor. Rewrite the transcript "
            "below:\n"
            "- remove filler words (umm, uh, أه, اه, اممم, and يعني when used "
            "as pure filler), false starts, and stutters\n"
            "- if the speaker corrects themselves mid-sentence (e.g. 'no "
            "wait', 'أقصد', 'I mean'), keep only the corrected version\n"
            "- fix grammar, punctuation and capitalization; restructure "
            "rambling run-ons into clear sentences without changing meaning\n"
            "- break into paragraphs when the topic shifts (phrases like 'on "
            "another note' start a new paragraph); if the speaker enumerates "
            "items ('number one…', 'first…', 'اول حاجة…'), format them as a "
            "numbered or bulleted list in the spoken order\n"
            "- keep the original language mix EXACTLY — Arabic stays in "
            "Arabic script, English words stay in English; never translate\n"
            "- never summarize, never answer questions contained in the "
            "text, never add content\n"
            f"- {tone}\n"
            + (f"- {ctx}\n" if ctx else "")
            + "Return ONLY the final text, no quotes, no commentary.\n\n"
            f"Transcript: {text}"
        )
        return self._chat(prompt)


class Api:
    """Thin JS bridge — pywebview exposes public members recursively, so this
    wrapper exposes ONLY the intended methods (its app ref is underscored)."""

    def __init__(self, app):
        self._app = app

    def get_init(self):
        return self._app.get_init()

    def save_key(self, key):
        return self._app.save_key(key)

    def set_setting(self, key, value):
        return self._app.set_setting(key, value)

    def set_language(self, lang):
        return self._app.set_language(lang)

    def copy_text(self, text):
        return self._app.copy_text(text)

    def export_history(self):
        return self._app.export_history()

    def open_link(self, url):
        return self._app.open_link(url)

    def do_update(self):
        return self._app.do_update()

    def check_update(self):
        return self._app.check_update()

    def undo_ai(self, ts):
        return self._app.undo_ai(ts)

    def retry_entry(self, ts):
        return self._app.retry_entry(ts)

    def extract_audio(self, ts):
        return self._app.extract_audio(ts)

    def delete_entry(self, ts):
        return self._app.delete_entry(ts)

    def quit(self):
        return self._app.quit()


class DialFlow:
    """Bridges the engine to the web UI."""

    def __init__(self):
        self.settings = Settings()
        self.engine = None
        self.main_win = None
        self.pill_win = None
        self.tray = None
        self.quitting = False
        # RLock: writers also mutate entries under it (retry un-fails a dict
        # while another thread may be serializing the same list)
        self._hist_lock = threading.RLock()
        self.history = self._load_history()

    # ---------- js api ----------

    def get_init(self):
        load_dotenv(ENV_FILE)
        key = os.environ.get("COHERE_API_KEY")
        logo = ""
        try:
            img = Image.open(ICON_FILE)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            logo = base64.b64encode(buf.getvalue()).decode()
        except Exception:
            pass
        if key and self.engine is None:
            self._boot_engine(key)
        user = (os.environ.get("USERNAME") or "").split(" ")[0].split(".")[0]
        return {
            "key_set": bool(key),
            "logo": logo,
            "user": user.capitalize() if user else "",
            "version": APP_VERSION,
            "settings": dict(self.settings),
            "history": self.history[-500:],
            "devices": self._devices(),
        }

    def save_key(self, key):
        key = (key or "").strip()
        try:
            r = requests.get(MODELS_URL,
                             headers={"Authorization": f"Bearer {key}"}, timeout=10)
            ok = r.status_code == 200
        except requests.RequestException:
            ok = False
        if not ok:
            return {"ok": False}
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write(f"COHERE_API_KEY={key}\n")
        os.environ["COHERE_API_KEY"] = key
        logging.info("api key saved on first run")
        self._boot_engine(key)
        return {"ok": True, "init": {
            "settings": dict(self.settings),
            "history": self.history[-500:],
            "devices": self._devices(),
        }}

    def set_setting(self, key, value):
        self.settings[key] = value
        self.settings.save()
        if key in ("rec_mode", "record_key", "lang_key", "command_key"):
            self._bind_hotkeys()
        elif key == "chime_volume" and self.engine:
            self.engine.rebuild_chimes()
        elif key == "autostart":
            _apply_autostart(bool(value))
        elif key == "theme":
            self._apply_titlebar()
        elif key == "idle_pill":
            if value:
                threading.Timer(0.1, self._show_pill_idle).start()
            elif self.engine is None or not self.engine.recording:
                self._pill_visible = False
                try:
                    self.pill_win.hide()
                except Exception:
                    pass
        return dict(self.settings)

    def set_language(self, lang):
        if self.engine:
            self.engine.language = lang
        self.settings["language"] = lang
        self.settings.save()

    def copy_text(self, text):
        pyperclip.copy(text)

    def export_history(self):
        if not self.history or self.main_win is None:
            return None
        path = self.main_win.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=f"dictations_{datetime.now():%Y-%m-%d}.txt")
        if not path:
            return None
        if isinstance(path, (list, tuple)):
            path = path[0]
        try:
            with open(path, "w", encoding="utf-8") as f:
                for e in self.history:
                    when = datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d %H:%M")
                    f.write(f"[{when}] ({e['lang']}) {e['text']}\n\n")
            return len(self.history)
        except OSError:
            logging.exception("export failed")
            return None

    def open_link(self, url):
        if url.startswith("https://"):
            webbrowser.open(url)

    # ---------- per-transcript actions ----------

    def _find_entry(self, ts):
        for e in self.history:
            if e.get("ts") == ts:
                return e
        return None

    def undo_ai(self, ts):
        e = self._find_entry(ts)
        if not e or not e.get("raw"):
            return None
        with self._hist_lock:
            e["text"] = e["raw"]
            e["words"] = len(e["text"].split())
            e["cleaned"] = False
            e["raw"] = ""
        self._save_history()
        return e

    def delete_entry(self, ts):
        e = self._find_entry(ts)
        if not e:
            return False
        with self._hist_lock:
            self.history.remove(e)
        if e.get("audio"):
            try:
                os.remove(os.path.join(AUDIO_DIR, e["audio"]))
            except OSError:
                pass
        self._save_history()
        return True

    def extract_audio(self, ts):
        e = self._find_entry(ts)
        if not e or not e.get("audio") or self.main_win is None:
            return None
        src = os.path.join(AUDIO_DIR, e["audio"])
        if not os.path.exists(src):
            return None
        stamp = datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d_%H%M")
        path = self.main_win.create_file_dialog(
            webview.SAVE_DIALOG, save_filename=f"dictation_{stamp}.wav")
        if not path:
            return None
        if isinstance(path, (list, tuple)):
            path = path[0]
        try:
            shutil.copyfile(src, path)
            return True
        except OSError:
            logging.exception("extract audio failed")
            return None

    def retry_entry(self, ts):
        e = self._find_entry(ts)
        if (not e or not e.get("audio") or self.engine is None
                or self.engine.recording):
            return False
        threading.Thread(target=self._retry_worker, args=(e,), daemon=True).start()
        return True

    def _retry_worker(self, e):
        path = os.path.join(AUDIO_DIR, e["audio"])
        self.engine._worker_state("transcribing", "")
        text = self.engine.transcribe_file(path, e.get("lang", "ar"))
        if not text:
            # re-push the entry so the feed row (and its Retry button)
            # renders back out of its 'Retrying…' state
            self._js(self.main_win,
                     f"app.updateEntry({json.dumps(e, ensure_ascii=False)})")
            self.engine._worker_state("error", "Retry failed — see app.log")
            return
        raw = text
        cleaned = False
        if self.settings.get("flow_mode", True):
            self.engine._worker_state("cleaning", "")
            out = self.engine._flow_clean(text)
            if out:
                text, cleaned = out, True
        text = _apply_dictionary(
            text, _parse_dictionary(self.settings.get("dictionary", "")))
        with self._hist_lock:
            e["text"] = text
            e["words"] = len(text.split())
            e["cleaned"] = cleaned
            e["raw"] = raw if cleaned else ""
            e.pop("failed", None)  # a successful retry un-fails the entry
        self._save_history()
        self._js(self.main_win,
                 f"app.updateEntry({json.dumps(e, ensure_ascii=False)})")
        self.engine._worker_state("idle", "Transcript updated")

    def quit(self):
        self._shutdown()

    # ---------- self-update ----------

    @staticmethod
    def _vtuple(s):
        nums = re.findall(r"\d+", s or "")
        return tuple(int(x) for x in nums[:3]) if nums else (0,)

    def _update_loop(self):
        """A launch check alone never reaches an app that stays open for days
        (or starts minimized to the tray and is never opened). Re-check on a
        schedule and announce through the tray, not just the in-app banner."""
        time.sleep(4)
        while not self.quitting:
            self._do_update_check()
            for _ in range(UPDATE_EVERY_H * 60):
                if self.quitting:
                    return
                time.sleep(60)

    def _do_update_check(self):
        """Returns a dict for the Settings 'Check now' button; also drives
        the banner + tray toast. Never raises."""
        try:
            r = requests.get(UPDATE_API, timeout=15,
                             headers={"User-Agent": "DialFlow"})
            if r.status_code != 200:
                logging.warning("update check http %s", r.status_code)
                return {"status": "error", "version": APP_VERSION}
            data = r.json()
            tag = data.get("tag_name", "")
            if self._vtuple(tag) <= self._vtuple(APP_VERSION):
                return {"status": "current", "version": APP_VERSION}
            url = next((a["browser_download_url"]
                        for a in data.get("assets", [])
                        if a.get("name", "").lower().endswith(".exe")), None)
            if not url:
                logging.warning("release %s has no .exe asset", tag)
                return {"status": "error", "version": APP_VERSION}
            self._update_url = url
            logging.info("update available: %s", tag)
            self._js(self.main_win, f"app.updateAvailable({json.dumps(tag)})")
            # toast once per version — the banner is invisible when the app
            # sits in the tray, which is where it spends most of its life
            if getattr(self, "_notified_tag", None) != tag:
                self._notified_tag = tag
                self._notify(f"Dial Flow {tag} is ready",
                             "Open Dial Flow and hit Update now — it installs "
                             "itself and restarts.")
            return {"status": "available", "tag": tag, "version": APP_VERSION}
        except Exception:
            logging.exception("update check failed")
            return {"status": "error", "version": APP_VERSION}

    def _notify(self, title, message):
        if self.tray is None:
            return
        try:
            self.tray.notify(message, title)
        except Exception:
            logging.exception("tray notify failed")

    def check_update(self):
        """Manual check from Settings / the tray menu."""
        return self._do_update_check()

    def _tray_check_update(self):
        def run():
            res = self._do_update_check()
            if res.get("status") == "current":
                self._notify("Dial Flow is up to date",
                             f"You're on {APP_VERSION}, the latest version.")
            elif res.get("status") == "error":
                self._notify("Couldn't check for updates",
                             "No connection to GitHub — see app.log.")
        threading.Thread(target=run, daemon=True).start()

    def do_update(self):
        if not getattr(self, "_update_url", None):
            return {"ok": False, "reason": "no update"}
        if not getattr(sys, "frozen", False):
            # running from source — swapping sys.executable would clobber
            # the Python interpreter itself
            return {"ok": False, "reason": "dev"}
        threading.Thread(target=self._update_worker, daemon=True).start()
        return {"ok": True}

    def _update_worker(self):
        """Download the new exe, then hand off to a script that swaps the
        file once this process exits and relaunches the app."""
        try:
            self._js(self.main_win, "app.updateState('downloading')")
            r = requests.get(self._update_url, timeout=600, stream=True)
            r.raise_for_status()
            new_path = os.path.join(CONFIG_DIR, "DialFlow_update.exe")
            with open(new_path, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
            if os.path.getsize(new_path) < 5_000_000:
                raise ValueError("downloaded file suspiciously small")
            cur = sys.executable
            bat = os.path.join(CONFIG_DIR, "update.bat")
            # the swap races this process's own exit — Windows keeps the exe
            # locked until every thread is gone, so retry for ~30s instead of
            # failing once and silently relaunching the OLD build
            with open(bat, "w", encoding="ascii") as f:
                f.write('@echo off\n'
                        'ping -n 4 127.0.0.1 >nul\n'
                        'set RETRY=0\n'
                        ':retry\n'
                        f'move /y "{new_path}" "{cur}" >nul 2>&1\n'
                        'if not errorlevel 1 goto done\n'
                        'set /a RETRY+=1\n'
                        'if %RETRY% GEQ 15 goto done\n'
                        'ping -n 3 127.0.0.1 >nul\n'
                        'goto retry\n'
                        ':done\n'
                        f'start "" "{cur}"\n'
                        'del "%~f0"\n')
            import subprocess
            subprocess.Popen(["cmd", "/c", bat],
                             creationflags=0x08000000)  # CREATE_NO_WINDOW
            logging.info("self-update handoff started")
            time.sleep(0.5)
            self._shutdown()
        except Exception:
            logging.exception("self-update failed")
            self._js(self.main_win, "app.updateState('failed')")

    # ---------- engine wiring ----------

    def _boot_engine(self, key):
        self.engine = Engine(key, self.settings, self._on_state,
                             self._on_transcript, self._on_language)
        self._bind_hotkeys()
        threading.Thread(target=self._level_pusher, daemon=True).start()
        threading.Thread(target=self._pill_follower, daemon=True).start()
        threading.Timer(1.5, self._show_pill_idle).start()

    def _bind_hotkeys(self):
        if self.engine is None:
            return
        keyboard.unhook_all()
        rk = self.settings["record_key"]
        lk = self.settings["lang_key"]
        ck = self.settings.get("command_key", "f8")
        if self.settings["rec_mode"] == "hold":
            keyboard.on_press_key(rk, lambda e: self.engine.start_recording(),
                                  suppress=False)
            keyboard.on_release_key(rk, lambda e: self.engine.stop_recording(),
                                    suppress=False)
            if ck and ck != rk:
                keyboard.on_press_key(ck, lambda e: self.engine.start_command(),
                                      suppress=False)
                keyboard.on_release_key(ck, lambda e: self.engine.stop_command(),
                                        suppress=False)
        else:
            keyboard.add_hotkey(rk, self.engine.toggle_recording, suppress=False)
            if ck and ck != rk:
                keyboard.add_hotkey(ck, self.engine.toggle_command,
                                    suppress=False)
        keyboard.add_hotkey(lk, self.engine.toggle_language, suppress=False)

    def _js(self, win, code):
        if win is None or self.quitting:
            return
        try:
            win.evaluate_js(code)
        except Exception:
            pass

    def _round_pill(self, attempt=0):
        """Round the pill via the DWM compositor — GDI window regions are
        ignored by WebView2's composited rendering, but DWM corner preference
        (the API behind every Win11 app's rounded corners) always applies.
        Must run after the first show(); hidden windows have no native form."""
        if getattr(self, "_pill_rounded", False):
            return
        try:
            h = self.pill_win.native.Handle
            phwnd = int(h.ToInt64()) if hasattr(h, "ToInt64") else int(h)
            # ROUNDSMALL for the idle bubble (8px ROUND needs 16px height and
            # ghosts past a 12px window); the animator switches to ROUND for
            # the expanded pill and back on collapse
            corner = ctypes.c_int(3)  # DWMWCP_ROUNDSMALL
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                phwnd, 33, ctypes.byref(corner), 4)
            # hide the OS border by painting it the pill's own background —
            # the DWMWA_COLOR_NONE sentinel renders as a LITERAL near-white
            # color on some builds (that was the ghost capsule around the
            # bubble). The design's hairline edge is drawn in CSS.
            border = ctypes.c_uint(0x00201317)  # COLORREF BGR of #171320
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                phwnd, 34, ctypes.byref(border), 4)
            # HUD behavior: WS_EX_NOACTIVATE so showing the pill can never
            # steal focus from the field the user is dictating into
            GWL_EXSTYLE = -20
            ex = ctypes.windll.user32.GetWindowLongPtrW(phwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongPtrW(
                phwnd, GWL_EXSTYLE,
                ex | 0x08000000 | 0x00000080)  # NOACTIVATE | TOOLWINDOW
            self._pill_hwnd = phwnd  # cached for the native animator
            try:
                # dark form backing — kills the white fringe that peeks out
                # around the web content at tiny sizes / during resizes.
                # NB: the assembly must be referenced before the namespace
                # import works under pythonnet — a bare import fails silently.
                import clr
                clr.AddReference("System.Drawing")
                import System.Drawing
                self.pill_win.native.BackColor = \
                    System.Drawing.ColorTranslator.FromHtml(PILL_BG)
            except Exception:
                logging.exception("pill backcolor failed")
            self._pill_rounded = True
        except Exception:
            if attempt < 4:
                threading.Timer(0.4, self._round_pill,
                                args=(attempt + 1,)).start()
            else:
                logging.exception("pill rounding failed")

    def _pill_anchor(self):
        """(center_x, bottom_y) on whichever monitor the cursor is on —
        Wispr-style multi-monitor follow, above that monitor's taskbar."""
        try:
            class POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            class MRECT(ctypes.Structure):
                _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                            ("r", ctypes.c_long), ("b", ctypes.c_long)]

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", MRECT),
                            ("rcWork", MRECT), ("dwFlags", ctypes.c_ulong)]

            u32 = ctypes.windll.user32
            pt = POINT()
            u32.GetCursorPos(ctypes.byref(pt))
            u32.MonitorFromPoint.argtypes = [POINT, ctypes.c_ulong]
            u32.MonitorFromPoint.restype = ctypes.c_void_p
            hmon = u32.MonitorFromPoint(pt, 2)  # MONITOR_DEFAULTTONEAREST
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            u32.GetMonitorInfoW(ctypes.c_void_p(hmon), ctypes.byref(mi))
            wk = mi.rcWork
            return wk.l + (wk.r - wk.l) // 2, wk.b - 18
        except Exception:
            return None

    def _move_pill(self):
        anchor = self._pill_anchor()
        if anchor and self.pill_win is not None:
            w, h = getattr(self, "_pill_wh", (MINI_W, MINI_H))
            try:
                self.pill_win.move(anchor[0] - w // 2, anchor[1] - h)
            except Exception:
                pass

    def _pill_follower(self):
        while not self.quitting:
            if getattr(self, "_pill_visible", False) and \
                    not getattr(self, "_pill_animating", False):
                self._move_pill()
            time.sleep(0.35)

    def _set_pill_corners(self, small):
        """DWM corner preference per state: ROUNDSMALL (4px) fits the 12px
        idle bubble; ROUND (8px) suits the 38px expanded pill. Using ROUND on
        the tiny window makes DWM draw corner arcs past its bounds."""
        hwnd = getattr(self, "_pill_hwnd", None)
        if not hwnd:
            return
        try:
            corner = ctypes.c_int(3 if small else 2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(corner), 4)
        except Exception:
            pass

    def _animate_pill(self, expand):
        """Eased window-size animation between the idle bubble and the full
        pill, anchored bottom-center so it grows in place. Drives raw
        SetWindowPos on the cached hwnd — pywebview's resize/move marshal
        through the UI thread and stutter, especially right after show()."""
        self._anim_token = getattr(self, "_anim_token", 0) + 1
        token = self._anim_token
        end = (PILL_W, PILL_H) if expand else (MINI_W, MINI_H)
        start = getattr(self, "_pill_wh", (MINI_W, MINI_H))
        if start == end:
            return
        anchor = self._pill_anchor()
        hwnd = getattr(self, "_pill_hwnd", None)
        if anchor is None or self.pill_win is None:
            self._pill_wh = end
            return
        self._pill_animating = True
        try:
            if expand:
                self._set_pill_corners(small=False)
                time.sleep(0.03)  # let the just-shown window settle first
            if hwnd:
                try:
                    dpi = ctypes.windll.user32.GetDpiForWindow(hwnd) / 96.0
                except Exception:
                    dpi = 1.0
                # design spec: grow 180ms cubic-bezier(.2,0,0,1) (strong
                # ease-out), shrink 200ms cubic-bezier(.4,0,.2,1) (in-out)
                steps = 16
                dt = (0.180 if expand else 0.200) / steps
                for i in range(1, steps + 1):
                    if token != self._anim_token or self.quitting:
                        return
                    t = i / steps
                    t = 1 - (1 - t) ** 3 if expand else t * t * (3 - 2 * t)
                    w = round(start[0] + (end[0] - start[0]) * t)
                    h = round(start[1] + (end[1] - start[1]) * t)
                    pw, ph = int(w * dpi), int(h * dpi)
                    ctypes.windll.user32.SetWindowPos(
                        hwnd, 0, anchor[0] - pw // 2, anchor[1] - ph, pw, ph,
                        0x0004 | 0x0010)  # SWP_NOZORDER | SWP_NOACTIVATE
                    self._pill_wh = (w, h)
                    time.sleep(dt)
            else:
                for i in range(1, 10):
                    if token != self._anim_token or self.quitting:
                        return
                    t = 1 - (1 - i / 9) ** 3
                    w = int(start[0] + (end[0] - start[0]) * t)
                    h = int(start[1] + (end[1] - start[1]) * t)
                    self.pill_win.resize(w, h)
                    self.pill_win.move(anchor[0] - w // 2, anchor[1] - h)
                    self._pill_wh = (w, h)
                    time.sleep(0.014)
            self._pill_wh = end
            # reconcile: the native SetWindowPos path resizes the form behind
            # WinForms' back, and a same-size framework resize is skipped as a
            # no-op — so jiggle by 1px first to force a real layout pass that
            # snaps the WebView2 content to the final bounds
            try:
                self.pill_win.resize(end[0] + 1, end[1] + 1)
                time.sleep(0.02)
                self.pill_win.resize(end[0], end[1])
                self.pill_win.move(anchor[0] - end[0] // 2, anchor[1] - end[1])
            except Exception:
                pass
            if not expand:
                self._set_pill_corners(small=True)
        except Exception:
            self._pill_wh = end
        finally:
            if token == self._anim_token:
                self._pill_animating = False

    def _pill_to_idle(self):
        """Direct settle (no success flash — error, toggle, etc.): empty the
        frame, shrink, then breathe the idle core back in."""
        if self.pill_win is None:
            return
        threading.Thread(target=self._pill_settle, args=(False,),
                         daemon=True).start()

    def _pill_settle(self, flash):
        """Design T3 timeline: flash 0-300ms → contents empty while the
        window shrinks 300-500ms → idle core fades in and breathes."""
        try:
            if flash:
                self._js(self.pill_win, "app.done()")
                time.sleep(0.30)
            if self.engine is not None and self.engine.recording:
                return  # a new recording started mid-flash — leave the pill
            if self.settings.get("idle_pill", True):
                self._js(self.pill_win, "app.mode('')")  # empty while moving
                self._pill_visible = True
                self._animate_pill(False)
                self._js(self.pill_win, "app.mode('mini')")  # coreIn 400ms
            else:
                self._pill_visible = False
                self.pill_win.hide()
        except Exception:
            pass

    def _show_pill_idle(self):
        """Startup: put the tiny idle bubble on screen (never activates)."""
        if self.pill_win is None or self.engine is None:
            return
        if not self.settings.get("idle_pill", True):
            return
        try:
            prev_fg = ctypes.windll.user32.GetForegroundWindow()
            self.pill_win.show()
            self._pill_wh = (PILL_W, PILL_H)
            self.pill_win.resize(MINI_W, MINI_H)
            self._pill_wh = (MINI_W, MINI_H)
            self._js(self.pill_win, "app.mode('mini')")
            self._move_pill()
            self._pill_visible = True
            threading.Timer(0.05, self._restore_focus, args=(prev_fg,)).start()
            threading.Timer(0.25, self._round_pill).start()
        except Exception:
            logging.exception("idle pill show failed")

    def _on_state(self, state, detail):
        started = self.engine.started_at if state == "recording" else 0
        self._js(self.main_win,
                 f"app.setState({json.dumps(state)}, {json.dumps(detail)}, "
                 f"{started or 0})")
        if self.pill_win is not None:
            try:
                if state == "recording":
                    # remember the user's focused window: showing the pill may
                    # activate it (first show, before NOACTIVATE applies) and
                    # would un-focus the field they want to dictate into
                    prev_fg = ctypes.windll.user32.GetForegroundWindow()
                    self._move_pill()
                    self.pill_win.show()
                    self._pill_visible = True
                    self._pill_processing = False
                    threading.Timer(0.05, self._restore_focus,
                                    args=(prev_fg,)).start()
                    threading.Timer(0.25, self._round_pill).start()
                    self._js(self.pill_win, f"app.start({started})")
                    threading.Thread(target=self._animate_pill, args=(True,),
                                     daemon=True).start()
                elif state in ("transcribing", "cleaning"):
                    # keep the pill expanded with a spinner until the text lands
                    self._pill_processing = True
                    self._js(self.pill_win, "app.mode('processing')")
                elif getattr(self, "_pill_processing", False) and state == "idle":
                    # designed T3 exit: success flash → empty shrink → breathe
                    self._pill_processing = False
                    threading.Thread(target=self._pill_settle, args=(True,),
                                     daemon=True).start()
                else:
                    self._pill_processing = False
                    self._pill_to_idle()
            except Exception:
                pass

    def _hide_pill(self):
        try:
            self.pill_win.hide()
        except Exception:
            pass

    @staticmethod
    def _restore_focus(prev_hwnd):
        try:
            if prev_hwnd and ctypes.windll.user32.GetForegroundWindow() != prev_hwnd:
                ctypes.windll.user32.SetForegroundWindow(prev_hwnd)
        except Exception:
            pass

    def _on_transcript(self, entry):
        with self._hist_lock:
            self.history.append(entry)
        self._save_history()
        self._js(self.main_win, f"app.addEntry({json.dumps(entry, ensure_ascii=False)})")

    def _on_language(self, lang):
        self._js(self.main_win, f"app.setLanguage({json.dumps(lang)})")

    def _level_pusher(self):
        while not self.quitting:
            if self.engine is not None and self.engine.recording:
                lv = round(self.engine.level, 4)
                self._js(self.main_win, f"app.setLevel({lv})")
                self._js(self.pill_win, f"app.level({lv})")
                time.sleep(0.04)
            else:
                time.sleep(0.15)

    # ---------- windows / tray ----------

    def _devices(self):
        try:
            return ["System default"] + sorted({
                d["name"] for d in sd.query_devices()
                if d["max_input_channels"] > 0})
        except Exception:
            return ["System default"]

    def _load_history(self):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def _save_history(self):
        """Serialized + atomic: concurrent workers (transcribe, retry, JS
        bridge) must never interleave writes, and a crash mid-write must
        never leave truncated JSON (which _load_history reads as 'no
        history' and silently starts over)."""
        with self._hist_lock:
            try:
                tmp = HISTORY_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.history[-500:], f, ensure_ascii=False,
                              indent=1)
                os.replace(tmp, HISTORY_FILE)
            except OSError:
                logging.exception("history save failed")

    def _setup_tray(self):
        try:
            img = Image.open(ICON_FILE)
            menu = pystray.Menu(
                pystray.MenuItem("Open Dial Flow",
                                 lambda: self._show_main(), default=True),
                pystray.MenuItem("Check for updates",
                                 lambda: self._tray_check_update()),
                pystray.MenuItem("Quit", lambda: self._shutdown()),
            )
            self.tray = pystray.Icon("DialFlow", img,
                                     "Dial Flow — F9 to record", menu)
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception:
            logging.exception("tray setup failed")
            self.tray = None

    def _show_main(self):
        try:
            self.main_win.show()
            self.main_win.restore()
        except Exception:
            pass

    def _on_closing(self):
        """Window X pressed: hide to tray instead of quitting."""
        if self.quitting or self.tray is None:
            return True
        try:
            self.main_win.hide()
            if not self.settings.get("_tray_tip_shown"):
                self.settings["_tray_tip_shown"] = True
                self.settings.save()
                try:
                    self.tray.notify("Still running — hotkeys stay active. "
                                     "Right-click the tray icon to quit.",
                                     "Dial Flow")
                except Exception:
                    pass
        except Exception:
            pass
        return False

    def _shutdown(self):
        if self.quitting:
            return
        self.quitting = True
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        for w in list(webview.windows):
            try:
                w.destroy()
            except Exception:
                pass

    def _theme_is_dark(self):
        pref = self.settings.get("theme", "system")
        if pref in ("dark", "light"):
            return pref == "dark"
        try:  # system: mirror Windows' app theme
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            light, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            winreg.CloseKey(key)
            return not light
        except OSError:
            return False

    def _apply_titlebar(self):
        """DWMWA_USE_IMMERSIVE_DARK_MODE on the main window, matched to the
        UI theme so the titlebar doesn't clash with the page."""
        if self.main_win is None:
            return
        try:
            h = self.main_win.native.Handle
            hwnd = int(h.ToInt64()) if hasattr(h, "ToInt64") else int(h)
            val = ctypes.c_int(1 if self._theme_is_dark() else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20,
                                                       ctypes.byref(val), 4)
        except Exception:
            logging.exception("titlebar theme failed")

    def _post_start(self):
        self._setup_tray()
        threading.Thread(target=self._update_loop, daemon=True).start()
        # keep the Run-key path fresh in case the exe was moved
        if self.settings.get("autostart") and getattr(sys, "frozen", False):
            _apply_autostart(True)
        # launched at login: start quietly in the tray
        if "--minimized" in sys.argv:
            try:
                self.main_win.hide()
            except Exception:
                pass
        # theme-matched titlebar + rounded pill region (best effort)
        self._apply_titlebar()
        # pill region is applied on first show — hidden pywebview windows
        # have no native form yet, so it can't be rounded here

    def run(self):
        sw = ctypes.windll.user32.GetSystemMetrics(0)
        sh = ctypes.windll.user32.GetSystemMetrics(1)
        self.main_win = webview.create_window(
            "Dial Flow", UI_FILE, js_api=Api(self),
            width=1160, height=780, min_size=(960, 640),
            background_color="#131316" if self._theme_is_dark() else "#FAFAF8")
        self.main_win.events.closing += self._on_closing
        self.pill_win = webview.create_window(
            "Dial Flow — recording", html=PILL_HTML,
            width=PILL_W, height=PILL_H, x=sw // 2 - PILL_W // 2, y=sh - 118,
            # override pywebview's silent 200x100 default min — it must be
            # allowed to shrink all the way down to the idle bubble
            min_size=(MINI_W, MINI_H),
            frameless=True, on_top=True, hidden=True, resizable=False,
            focus=False, background_color=PILL_BG)
        logging.info("app started")
        webview.start(func=self._post_start, debug=False)


def main():
    ctypes.windll.kernel32.CreateMutexW(None, False, "DialFlow.Singleton")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(
            None, "Dial Flow is already running — check your taskbar or "
                  "system tray.", "Dial Flow", 0x40)
        return
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
        "Dialverse.DialFlow")
    DialFlow().run()


if __name__ == "__main__":
    main()
