"""ShareScreen - compartilha uma janela ou a tela inteira, com o áudio do PC,
pela rede local através de um servidor web.

    python sharescreen.py [--port 8080]

Escolha na janela o que compartilhar e clique em "Iniciar compartilhamento".
Quem estiver na mesma rede assiste abrindo http://<ip-deste-pc>:8080 no navegador.

Somente Windows. Dependências: pip install -r requirements.txt
"""

import argparse
import contextlib
import ctypes
import io
import json
import os
import queue
import secrets
import select
import socket
import sys
import threading
import time
import tkinter as tk
import webbrowser
from array import array
from ctypes import wintypes
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tkinter import messagebox, ttk

from PIL import Image, ImageDraw, ImageFont

try:
    import pyaudiowpatch as pyaudio  # PyAudio com captura de loopback (WASAPI)
except ImportError:
    pyaudio = None

if sys.platform != "win32":
    sys.exit("O ShareScreen funciona apenas no Windows.")

QUALITIES = [
    # (texto na interface, largura máxima em pixels, qualidade JPEG, quadros por segundo)
    ("Alta (original, 20 fps)", None, 80, 20),
    ("Média (1600 px, 15 fps)", 1600, 70, 15),
    ("Baixa (1024 px, 10 fps)", 1024, 55, 10),
]


# ---------------------------------------------------------------------------
# Win32
# ---------------------------------------------------------------------------

# Sem isto o Windows devolve coordenadas falsas em telas com escala (125%, 150%...)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except (AttributeError, OSError):
    ctypes.windll.user32.SetProcessDPIAware()

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
PW_RENDERFULLCONTENT = 2
DI_NORMAL = 3
CURSOR_SHOWING = 1
GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x80
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
MONITORINFOF_PRIMARY = 1


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HICON),
        ("ptScreenPos", wintypes.POINT),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)

user32 = ctypes.WinDLL("user32")
gdi32 = ctypes.WinDLL("gdi32")
dwmapi = ctypes.WinDLL("dwmapi")
INT = ctypes.c_int

# Os tipos precisam ser declarados: em 64 bits os handles não cabem no int padrão do ctypes
for _function, _restype, _argtypes in [
    (user32.GetDC, wintypes.HDC, [wintypes.HWND]),
    (user32.ReleaseDC, ctypes.c_int, [wintypes.HWND, wintypes.HDC]),
    (user32.PrintWindow, wintypes.BOOL, [wintypes.HWND, wintypes.HDC, wintypes.UINT]),
    (user32.GetWindowRect, wintypes.BOOL, [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]),
    (user32.IsWindow, wintypes.BOOL, [wintypes.HWND]),
    (user32.IsWindowVisible, wintypes.BOOL, [wintypes.HWND]),
    (user32.IsIconic, wintypes.BOOL, [wintypes.HWND]),
    (user32.GetWindowTextLengthW, ctypes.c_int, [wintypes.HWND]),
    (user32.GetWindowTextW, ctypes.c_int, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]),
    (user32.GetWindowLongW, wintypes.LONG, [wintypes.HWND, ctypes.c_int]),
    (user32.GetWindowThreadProcessId, wintypes.DWORD, [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]),
    (user32.EnumWindows, wintypes.BOOL, [WNDENUMPROC, wintypes.LPARAM]),
    (
        user32.EnumDisplayMonitors,
        wintypes.BOOL,
        [wintypes.HDC, ctypes.POINTER(wintypes.RECT), MONITORENUMPROC, wintypes.LPARAM],
    ),
    (user32.GetMonitorInfoW, wintypes.BOOL, [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]),
    (user32.GetCursorInfo, wintypes.BOOL, [ctypes.POINTER(CURSORINFO)]),
    (user32.GetIconInfo, wintypes.BOOL, [wintypes.HICON, ctypes.POINTER(ICONINFO)]),
    (
        user32.DrawIconEx,
        wintypes.BOOL,
        [wintypes.HDC, INT, INT, wintypes.HICON, INT, INT, wintypes.UINT, wintypes.HBRUSH, wintypes.UINT],
    ),
    (user32.WindowFromPoint, wintypes.HWND, [wintypes.POINT]),
    (user32.GetAncestor, wintypes.HWND, [wintypes.HWND, wintypes.UINT]),
    (gdi32.CreateCompatibleDC, wintypes.HDC, [wintypes.HDC]),
    (
        gdi32.CreateDIBSection,
        wintypes.HBITMAP,
        [wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
         wintypes.HANDLE, wintypes.DWORD],
    ),
    (gdi32.SelectObject, wintypes.HGDIOBJ, [wintypes.HDC, wintypes.HGDIOBJ]),
    (gdi32.DeleteObject, wintypes.BOOL, [wintypes.HGDIOBJ]),
    (gdi32.DeleteDC, wintypes.BOOL, [wintypes.HDC]),
    (gdi32.BitBlt, wintypes.BOOL, [wintypes.HDC, INT, INT, INT, INT, wintypes.HDC, INT, INT, wintypes.DWORD]),
    (gdi32.GdiFlush, wintypes.BOOL, []),
    (
        dwmapi.DwmGetWindowAttribute,
        ctypes.c_long,
        [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD],
    ),
]:
    _function.restype, _function.argtypes = _restype, _argtypes


# ---------------------------------------------------------------------------
# O que pode ser compartilhado
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    """Algo que pode ser compartilhado: uma tela (rect) ou uma janela (hwnd)."""

    label: str = field(compare=False)
    rect: tuple = None  # (esquerda, topo, direita, base) em pixels, para telas
    hwnd: int = None  # identificador da janela


def list_screens():
    found = []

    @MONITORENUMPROC
    def on_monitor(monitor, dc, rect, param):
        info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            r = info.rcMonitor
            found.append((not info.dwFlags & MONITORINFOF_PRIMARY, r.left, r.top, r.right, r.bottom))
        return True

    user32.EnumDisplayMonitors(None, None, on_monitor, 0)
    found.sort()  # a principal primeiro, depois da esquerda para a direita
    rects = [monitor[1:] for monitor in found]

    def size(rect):
        return f"{rect[2] - rect[0]}×{rect[3] - rect[1]}"

    if len(rects) == 1:
        return [Source(f"Tela inteira ({size(rects[0])})", rect=rects[0])]
    screens = [
        Source(f"Tela {number}{' principal' if number == 1 else ''} ({size(rect)})", rect=rect)
        for number, rect in enumerate(rects, 1)
    ]
    if rects:
        everything = (
            min(r[0] for r in rects),
            min(r[1] for r in rects),
            max(r[2] for r in rects),
            max(r[3] for r in rects),
        )
        screens.append(Source(f"Todas as telas ({size(everything)})", rect=everything))
    return screens


def list_windows():
    """Janelas visíveis de outros programas, da mais recente para a mais antiga."""
    found = []

    @WNDENUMPROC
    def on_window(hwnd, param):
        length = user32.GetWindowTextLengthW(hwnd)
        if not length or not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        # Janelas "cloaked" existem mas não aparecem (apps suspensos, outras áreas de trabalho)
        cloaked = wintypes.DWORD()
        dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if cloaked.value:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid():
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        if title.value != "Program Manager":  # a área de trabalho
            found.append(Source(f"Janela: {title.value}", hwnd=hwnd))
        return True

    user32.EnumWindows(on_window, 0)
    return found


# ---------------------------------------------------------------------------
# Captura de vídeo
# ---------------------------------------------------------------------------

class CaptureError(Exception):
    """Não há imagem para capturar; a mensagem é exibida aos espectadores."""


class Grabber:
    """Captura telas e janelas via GDI. Deve ser usado sempre pela mesma thread."""

    def __init__(self):
        self._screen_dc = user32.GetDC(None)
        self._mem_dc = gdi32.CreateCompatibleDC(self._screen_dc)
        self._size = None
        self._bitmap = self._default_bitmap = None
        self._bits = ctypes.c_void_p()

    def close(self):
        self._resize(None)
        gdi32.DeleteDC(self._mem_dc)
        user32.ReleaseDC(None, self._screen_dc)

    def _resize(self, size):
        if size == self._size:
            return
        if self._bitmap:
            gdi32.SelectObject(self._mem_dc, self._default_bitmap)
            gdi32.DeleteObject(self._bitmap)
            self._bitmap = None
        self._size = None
        if size is None:
            return
        info = BITMAPINFO()
        header = info.bmiHeader
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth, header.biHeight = size[0], -size[1]  # altura negativa: linhas de cima para baixo
        header.biPlanes, header.biBitCount = 1, 32
        self._bitmap = gdi32.CreateDIBSection(
            self._screen_dc, ctypes.byref(info), 0, ctypes.byref(self._bits), None, 0
        )
        if not self._bitmap:
            raise CaptureError("Não foi possível capturar a imagem")
        self._default_bitmap = gdi32.SelectObject(self._mem_dc, self._bitmap)
        self._size = size

    def _draw_cursor(self, left, top, over=None):
        """Desenha o ponteiro do mouse; com `over`, só se ele estiver sobre essa janela."""
        cursor = CURSORINFO(cbSize=ctypes.sizeof(CURSORINFO))
        if not user32.GetCursorInfo(ctypes.byref(cursor)) or not cursor.flags & CURSOR_SHOWING:
            return
        position = cursor.ptScreenPos
        if over is not None and user32.GetAncestor(user32.WindowFromPoint(position), GA_ROOT) != over:
            return
        icon = ICONINFO()
        if user32.GetIconInfo(cursor.hCursor, ctypes.byref(icon)):
            # GetIconInfo cria cópias dos bitmaps, que precisam ser liberadas
            for bitmap in (icon.hbmMask, icon.hbmColor):
                if bitmap:
                    gdi32.DeleteObject(bitmap)
        user32.DrawIconEx(
            self._mem_dc,
            position.x - left - icon.xHotspot,
            position.y - top - icon.yHotspot,
            cursor.hCursor,
            0,
            0,
            0,
            None,
            DI_NORMAL,
        )

    def grab(self, source):
        """Devolve (pixels BGRX, tamanho, recorte) da fonte ou levanta CaptureError."""
        crop = None
        if source.hwnd is None:
            left, top, right, bottom = source.rect
            width, height = right - left, bottom - top
            self._resize((width, height))
            gdi32.BitBlt(self._mem_dc, 0, 0, width, height, self._screen_dc, left, top, SRCCOPY | CAPTUREBLT)
            self._draw_cursor(left, top)
        else:
            hwnd = source.hwnd
            if not user32.IsWindow(hwnd):
                raise CaptureError("A janela compartilhada foi fechada")
            if user32.IsIconic(hwnd):
                raise CaptureError("A janela compartilhada está minimizada")
            full, visible = wintypes.RECT(), wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(full))
            width, height = full.right - full.left, full.bottom - full.top
            if width <= 0 or height <= 0:
                raise CaptureError("A janela compartilhada não está visível")
            self._resize((width, height))
            # PrintWindow funciona mesmo com a janela encoberta; se falhar, copia a região da tela
            if not user32.PrintWindow(hwnd, self._mem_dc, PW_RENDERFULLCONTENT):
                gdi32.BitBlt(
                    self._mem_dc, 0, 0, width, height, self._screen_dc, full.left, full.top, SRCCOPY | CAPTUREBLT
                )
            self._draw_cursor(full.left, full.top, over=hwnd)
            # GetWindowRect inclui as bordas invisíveis de redimensionamento: recorta a parte visível
            if not dwmapi.DwmGetWindowAttribute(
                hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(visible), ctypes.sizeof(visible)
            ):
                box = (
                    max(visible.left - full.left, 0),
                    max(visible.top - full.top, 0),
                    min(visible.right - full.left, width),
                    min(visible.bottom - full.top, height),
                )
                if box[0] < box[2] and box[1] < box[3]:
                    crop = box
        gdi32.GdiFlush()
        return ctypes.string_at(self._bits.value, width * height * 4), (width, height), crop


def message_image(text):
    """Quadro de aviso para quando não há o que capturar (ex.: janela minimizada)."""
    image = Image.new("RGB", (960, 540), (11, 12, 15))
    try:
        font = ImageFont.truetype("segoeui.ttf", 30)
    except OSError:
        font = ImageFont.load_default(30)
    ImageDraw.Draw(image).text((480, 270), text, font=font, fill=(200, 202, 210), anchor="mm")
    return image


def encode_jpeg(shot, max_width, quality):
    """Converte o resultado de Grabber.grab (ou uma mensagem de aviso) em JPEG."""
    if isinstance(shot, str):
        image = message_image(shot)
    else:
        pixels, size, crop = shot
        image = Image.frombuffer("RGB", size, pixels, "raw", "BGRX", 0, 1)
        if crop:
            image = image.crop(crop)
    if max_width and image.width > max_width:
        height = max(1, round(image.height * max_width / image.width))
        image = image.resize((max_width, height), Image.Resampling.BILINEAR)
    output = io.BytesIO()
    image.save(output, "JPEG", quality=quality)
    return output.getvalue()


class VideoHub:
    """Thread que captura a fonte escolhida e entrega o quadro mais recente aos espectadores.

    `source` e `quality` podem ser trocados a qualquer momento, sem parar a transmissão.
    """

    def __init__(self, source, quality):
        self.source = source
        self.quality = quality
        self.viewers = 0
        self._frame = None
        self._frame_id = 0
        self._changed = threading.Condition()
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._closed.set()
        with self._changed:
            self._changed.notify_all()
        self._thread.join(timeout=2)

    @contextlib.contextmanager
    def viewer(self):
        with self._changed:
            self.viewers += 1
        try:
            yield
        finally:
            with self._changed:
                self.viewers -= 1

    def wait_frame(self, last_id):
        """Espera (até 1 s) por um quadro diferente de `last_id`; devolve (id, jpeg)."""
        with self._changed:
            if self._frame is None or self._frame_id == last_id:
                self._changed.wait(1.0)
            return self._frame_id, self._frame

    def _publish(self, jpeg):
        with self._changed:
            self._frame = jpeg
            self._frame_id += 1
            self._changed.notify_all()

    def _run(self):
        grabber = Grabber()
        last_shot = last_setup = jpeg = None
        last_sent = 0.0
        try:
            while not self._closed.is_set():
                if not self.viewers:  # ninguém assistindo: não gasta CPU
                    last_shot = self._frame = None
                    self._closed.wait(0.2)
                    continue
                started = time.perf_counter()
                setup = self.source, self.quality
                source, (_, max_width, jpeg_quality, fps) = setup
                try:
                    shot = grabber.grab(source)
                except CaptureError as error:
                    shot = str(error)
                # Só recodifica quando a imagem muda; sem mudanças, reenvia o mesmo quadro
                # 1 vez por segundo para manter a conexão viva e perceber quem saiu
                changed = shot != last_shot or setup != last_setup
                if changed:
                    jpeg = encode_jpeg(shot, max_width, jpeg_quality)
                    last_shot, last_setup = shot, setup
                if changed or started - last_sent >= 1.0:
                    self._publish(jpeg)
                    last_sent = started
                # time.sleep, e não Event.wait: no Windows o wait arredonda para múltiplos de ~16 ms
                time.sleep(max(0.0, 1 / fps - (time.perf_counter() - started)))
        finally:
            grabber.close()


# ---------------------------------------------------------------------------
# Captura de áudio
# ---------------------------------------------------------------------------

class AudioHub:
    """Captura o som que sai pelos alto-falantes (loopback WASAPI) e o distribui como PCM de 16 bits."""

    def __init__(self):
        self._listeners = set()
        self._lock = threading.Lock()
        self._pa = pyaudio.PyAudio()
        try:
            device = self._pa.get_default_wasapi_loopback()
            self._device_channels = device["maxInputChannels"]
            self.channels = min(self._device_channels, 2)
            self.rate = int(device["defaultSampleRate"])
            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=self._device_channels,
                rate=self.rate,
                input=True,
                input_device_index=device["index"],
                frames_per_buffer=self.rate // 50,
                stream_callback=self._on_audio,
            )
        except Exception:
            self._pa.terminate()
            raise

    def stop(self):
        self._stream.stop_stream()
        self._stream.close()
        self._pa.terminate()

    @contextlib.contextmanager
    def listener(self):
        """Fila que recebe os blocos de áudio enquanto o espectador estiver conectado."""
        chunks = queue.Queue(maxsize=50)  # ~1 s; além disso o espectador está lento e perde áudio
        with self._lock:
            self._listeners.add(chunks)
        try:
            yield chunks
        finally:
            with self._lock:
                self._listeners.discard(chunks)

    def _on_audio(self, data, frame_count, time_info, status):
        if self._device_channels > 2:  # 5.1/7.1: mantém só os canais frontais esquerdo e direito
            samples = array("h", data)
            stereo = array("h", bytes(frame_count * 4))
            stereo[0::2] = samples[0 :: self._device_channels]
            stereo[1::2] = samples[1 :: self._device_channels]
            data = stereo.tobytes()
        with self._lock:
            for chunks in self._listeners:
                if chunks.full():
                    with contextlib.suppress(queue.Empty):
                        chunks.get_nowait()
                chunks.put_nowait(data)
        return None, pyaudio.paContinue


# ---------------------------------------------------------------------------
# Servidor web
# ---------------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ShareScreen</title>
<style>
  html, body { height: 100%; margin: 0; overflow: hidden; background: #0b0c0f; color: #e8e8ea;
               font: 15px system-ui, "Segoe UI", sans-serif; }
  #view { position: fixed; inset: 0; width: 100%; height: 100%; object-fit: contain; }
  #msg { position: fixed; inset: 0; display: flex; align-items: center; justify-content: center;
         padding: 24px; text-align: center; font-size: 1.15rem; color: #a9abb3; }
  #bar { position: fixed; left: 50%; bottom: 18px; transform: translateX(-50%);
         display: flex; align-items: center; gap: 10px; padding: 8px 10px;
         background: rgba(24, 26, 32, .9); border: 1px solid #2c2f38; border-radius: 12px;
         transition: opacity .25s; }
  body.idle { cursor: none; }
  body.idle #bar { opacity: 0; pointer-events: none; }
  button { font: inherit; color: inherit; background: #2a2d37; border: 0; border-radius: 8px;
           padding: 8px 14px; cursor: pointer; white-space: nowrap; }
  button:hover { background: #363a47; }
  button.on { background: #1f6feb; }
  input[type=range] { width: 110px; accent-color: #1f6feb; }
  #note { padding: 0 6px; color: #a9abb3; white-space: nowrap; }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<img id="view" alt="" hidden>
<div id="msg">Conectando…</div>
<div id="bar">
  <button id="audio" hidden>🔇 Clique para ativar o áudio</button>
  <input id="volume" type="range" min="0" max="1" step="0.01" value="1" title="Volume" hidden>
  <span id="note" hidden>Áudio desativado neste PC para evitar eco</span>
  <button id="full">Tela cheia</button>
</div>
<script>
const view = document.getElementById('view');
const msg = document.getElementById('msg');
const audioBtn = document.getElementById('audio');
const volume = document.getElementById('volume');
const fullBtn = document.getElementById('full');

const BUFFER = 0.12;     // segundos de áudio guardados antes de tocar (absorve oscilações da rede)
const MAX_AHEAD = 0.6;   // acima disso o áudio é descartado para não acumular atraso

let info = null;         // último /status recebido; null = sem transmissão
let audioOn = false;
let audioAsked = false;  // o espectador já ligou ou desligou o áudio alguma vez?
let audioCtx = null, gain = null, audioFetch = null;

function showMessage(text) {
  msg.textContent = text;
  msg.hidden = !text;
  view.hidden = !!text;
}

async function getStatus() {
  const control = new AbortController();
  const timer = setTimeout(() => control.abort(), 3000);
  try {
    const response = await fetch('/status', {cache: 'no-store', signal: control.signal});
    return response.ok ? await response.json() : null;
  } catch (error) {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

// Verifica periodicamente se a transmissão começou, parou ou foi reiniciada
async function poll() {
  const status = await getStatus();
  if (!status) {
    if (info) { view.removeAttribute('src'); stopAudio(); }
    info = null;
    showMessage('Sem transmissão no momento. Aguardando…');
  } else if (!info || info.session !== status.session) {
    info = status;
    view.src = '/stream?s=' + status.session;
    showMessage('');
    audioBtn.hidden = !status.audio;
    volume.hidden = !(status.audio && audioOn);
    document.getElementById('note').hidden = !status.echo_guard;
    if (audioOn && status.audio) playAudio(); else stopAudio();
  }
  setTimeout(poll, 2000);
}
view.onerror = () => { info = null; };   // a conexão do vídeo caiu: reconecta no próximo poll

function stopAudio() {
  if (audioFetch) audioFetch.abort();
  audioFetch = null;
}

// Recebe PCM de 16 bits por HTTP e agenda cada bloco para tocar logo após o anterior
async function playAudio() {
  stopAudio();
  const control = audioFetch = new AbortController();
  const rate = info.rate, channels = info.channels, frameBytes = 2 * channels;
  if (!audioCtx || audioCtx.sampleRate !== rate) {
    if (audioCtx) audioCtx.close();
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    try { audioCtx = new AudioCtx({sampleRate: rate}); } catch (error) { audioCtx = new AudioCtx(); }
    gain = audioCtx.createGain();
    gain.gain.value = volume.value;
    gain.connect(audioCtx.destination);
  }
  audioCtx.resume();
  try {
    const response = await fetch('/audio?s=' + info.session, {cache: 'no-store', signal: control.signal});
    if (!response.ok) throw new Error('sem áudio');
    const reader = response.body.getReader();
    let rest = new Uint8Array(0), nextTime = 0;
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      // A rede corta os dados em qualquer ponto: junta a sobra do pedaço anterior
      let data = value;
      if (rest.length) {
        data = new Uint8Array(rest.length + value.length);
        data.set(rest);
        data.set(value, rest.length);
      }
      const frames = Math.floor(data.length / frameBytes);
      rest = data.slice(frames * frameBytes);
      if (!frames) continue;
      const now = audioCtx.currentTime;
      if (nextTime < now + 0.01) nextTime = now + BUFFER;   // início, ou faltou áudio
      if (nextTime > now + MAX_AHEAD) continue;              // atrasado demais: descarta
      const pcm = new DataView(data.buffer, data.byteOffset, frames * frameBytes);
      const buffer = audioCtx.createBuffer(channels, frames, rate);
      for (let c = 0; c < channels; c++) {
        const out = buffer.getChannelData(c);
        for (let i = 0; i < frames; i++) out[i] = pcm.getInt16((i * channels + c) * 2, true) / 32768;
      }
      const node = audioCtx.createBufferSource();
      node.buffer = buffer;
      node.connect(gain);
      node.start(nextTime);
      nextTime += frames / rate;
    }
  } catch (error) { /* cancelado por stopAudio() ou transmissão encerrada */ }
  if (audioFetch === control) {   // caiu sem ter sido cancelado: tenta de novo
    audioFetch = null;
    setTimeout(() => { if (audioOn && info && info.audio && !audioFetch) playAudio(); }, 1500);
  }
}

function setAudio(on) {
  audioAsked = true;
  audioOn = on;
  audioBtn.textContent = on ? '🔊 Áudio ligado' : '🔇 Áudio desligado';
  audioBtn.classList.toggle('on', on);
  volume.hidden = !on;
  if (on) playAudio();
  else { stopAudio(); if (audioCtx) audioCtx.suspend(); }
}
audioBtn.onclick = () => setAudio(!audioOn);
volume.oninput = () => { if (gain) gain.gain.value = volume.value; };
// Os navegadores só liberam som depois de um clique: o primeiro clique na página já liga o áudio
document.addEventListener('click', () => { if (!audioAsked && info && info.audio) setAudio(true); });

if (!document.documentElement.requestFullscreen) fullBtn.hidden = true;
fullBtn.onclick = () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen();
};

// Esconde a barra e o cursor após 3 s parado (mas não enquanto o áudio espera um clique)
let idleTimer;
function wake() {
  document.body.classList.remove('idle');
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => {
    if (audioAsked || !(info && info.audio)) document.body.classList.add('idle');
  }, 3000);
}
['mousemove', 'mousedown', 'touchstart', 'keydown'].forEach(name => addEventListener(name, wake));

wake();
poll();
</script>
</body>
</html>
""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    timeout = 15  # segundos; derruba espectadores cuja conexão travou

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        session = self.server.session
        route = self.path.split("?")[0]
        # Se a página tocasse o som neste mesmo PC, ele seria capturado de novo, em eco sem fim:
        # para quem assiste daqui vai só o vídeo
        echo_guard = session.audio is not None and self._same_pc()
        audio = None if echo_guard else session.audio
        try:
            if route == "/":
                self._reply(200, "text/html; charset=utf-8", PAGE)
            elif route == "/status":
                status = {
                    "session": session.id,
                    "audio": audio is not None,
                    "echo_guard": echo_guard,
                    "rate": audio.rate if audio else 0,
                    "channels": audio.channels if audio else 0,
                }
                self._reply(200, "application/json", json.dumps(status).encode())
            elif route == "/stream":
                self._send_video(session)
            elif route == "/audio" and audio:
                self._send_audio(session)
            else:
                self._reply(404, "text/plain; charset=utf-8", "Não encontrado".encode())
        except OSError:
            pass  # o espectador fechou a página

    def _same_pc(self):
        """O espectador está neste PC? (a conexão sai e chega no mesmo endereço)"""
        return self.client_address[0] == self.connection.getsockname()[0]

    def _reply(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _start_stream(self, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _send_video(self, session):
        """Vídeo como MJPEG: uma sequência sem fim de JPEGs que o navegador exibe em um <img>."""
        self._start_stream("multipart/x-mixed-replace; boundary=frame")
        self.wfile.write(b"--frame\r\n")
        last_id = None
        with session.video.viewer():
            while not session.closed.is_set():
                frame_id, jpeg = session.video.wait_frame(last_id)
                if jpeg is None or frame_id == last_id:
                    continue
                last_id = frame_id
                # O separador do próximo quadro vai junto: o Chrome só exibe a imagem ao recebê-lo
                self.wfile.write(
                    b"Content-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n%b\r\n--frame\r\n" % (len(jpeg), jpeg)
                )

    def _send_audio(self, session):
        """Áudio como PCM bruto (16 bits, intercalado), tocado pela página via Web Audio."""
        self._start_stream("application/octet-stream")
        with session.audio.listener() as chunks:
            while not session.closed.is_set():
                try:
                    self.wfile.write(chunks.get(timeout=1.0))
                except queue.Empty:
                    # Em silêncio o Windows não entrega áudio e nada é enviado;
                    # confere se o espectador ainda está conectado
                    readable, _, _ = select.select([self.connection], [], [], 0)
                    if readable and not self.connection.recv(1, socket.MSG_PEEK):
                        break


class Server(ThreadingHTTPServer):
    # No Windows, True deixaria abrir a porta mesmo já estando em uso por outro programa
    allow_reuse_address = False


class ShareSession:
    """Uma transmissão: captura de vídeo, de áudio e o servidor web. Criar inicia; close() encerra."""

    def __init__(self, port, source, quality, with_audio):
        self.id = secrets.token_hex(4)  # muda a cada transmissão; a página usa para reconectar
        self.closed = threading.Event()
        self._httpd = Server(("0.0.0.0", port), Handler)  # OSError se a porta estiver ocupada
        self._httpd.session = self
        self.audio = None
        self.audio_error = None
        if with_audio:
            try:
                self.audio = AudioHub()
            except (OSError, LookupError) as error:
                self.audio_error = str(error) or type(error).__name__
        self.video = VideoHub(source, quality)
        threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()

    def close(self):
        self.closed.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        self.video.stop()
        if self.audio:
            self.audio.stop()


def local_ip():
    """Endereço deste PC na rede local."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))  # UDP: nada é enviado, só descobre a interface de saída
            return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

IDLE_TEXT = "Parado. Escolha o que compartilhar e clique em Iniciar."


class App(tk.Tk):
    def __init__(self, port):
        super().__init__()
        self.port = port
        self.session = None
        self.sources = []
        self.url = ""
        self.title("ShareScreen")
        self._build()
        self.refresh()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._tick()

    def _build(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root)
        header.pack(fill="x")
        ttk.Label(header, text="O que você quer compartilhar?").pack(side="left")
        ttk.Button(header, text="Atualizar lista", command=self.refresh).pack(side="right")

        # Empacotados antes da lista para que, ao redimensionar, só ela encolha
        address = ttk.Frame(root)
        address.pack(side="bottom", fill="x", pady=(4, 0))
        self.url_label = ttk.Label(address, foreground="#0a58ca", cursor="hand2")
        self.url_label.pack(side="left")
        self.url_label.bind("<Button-1>", lambda event: self.url and webbrowser.open(self.url))
        self.copy_button = ttk.Button(address, text="Copiar endereço", command=self.copy_url, state="disabled")
        self.copy_button.pack(side="right")

        self.status = ttk.Label(root, text=IDLE_TEXT)
        self.status.pack(side="bottom", anchor="w")

        self.toggle_button = ttk.Button(root, text="Iniciar compartilhamento", command=self.toggle)
        self.toggle_button.pack(side="bottom", fill="x", pady=(12, 8), ipady=4)

        options = ttk.Frame(root)
        options.pack(side="bottom", fill="x")
        self.audio_on = tk.BooleanVar(value=pyaudio is not None)
        self.audio_check = ttk.Checkbutton(options, text="Compartilhar o áudio do PC", variable=self.audio_on)
        if pyaudio is None:
            self.audio_check.configure(text="Áudio indisponível (instale PyAudioWPatch)", state="disabled")
        self.audio_check.pack(side="left")
        self.quality_box = ttk.Combobox(
            options, state="readonly", width=24, values=[quality[0] for quality in QUALITIES]
        )
        self.quality_box.current(0)
        self.quality_box.pack(side="right")
        self.quality_box.bind("<<ComboboxSelected>>", self.on_quality)
        ttk.Label(options, text="Qualidade:").pack(side="right", padx=(16, 6))

        box = ttk.Frame(root)
        box.pack(fill="both", expand=True, pady=8)
        self.listbox = tk.Listbox(box, width=64, height=12, exportselection=False, activestyle="none")
        scrollbar = ttk.Scrollbar(box, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.listbox.pack(side="left", fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self.on_select)
        self.listbox.bind("<Double-Button-1>", lambda event: None if self.session else self.start())

    def selected(self):
        picked = self.listbox.curselection()
        return self.sources[picked[0]] if picked else None

    def quality(self):
        return QUALITIES[self.quality_box.current()]

    def refresh(self):
        current = self.session.video.source if self.session else self.selected()
        self.sources = list_screens() + list_windows()
        self.listbox.delete(0, "end")
        for source in self.sources:
            self.listbox.insert("end", " " + source.label)
        if current in self.sources:
            index = self.sources.index(current)
        elif self.session or not self.sources:
            return  # a janela transmitida sumiu da lista: não seleciona outra por conta própria
        else:
            index = 0
        self.listbox.selection_set(index)
        self.listbox.see(index)

    def on_select(self, event=None):
        source = self.selected()
        if self.session and source:
            self.session.video.source = source  # troca a fonte sem interromper a transmissão

    def on_quality(self, event=None):
        if self.session:
            self.session.video.quality = self.quality()

    def toggle(self):
        if self.session:
            self.stop()
        else:
            self.start()

    def start(self):
        source = self.selected()
        if source is None:
            messagebox.showinfo("ShareScreen", "Escolha na lista o que deseja compartilhar.")
            return
        try:
            self.session = ShareSession(self.port, source, self.quality(), self.audio_on.get())
        except OSError as error:
            messagebox.showerror(
                "ShareScreen", f"Não foi possível iniciar o servidor na porta {self.port}:\n{error}"
            )
            return
        self.url = f"http://{local_ip()}:{self.port}"
        self.url_label.configure(text=self.url)
        self.copy_button.configure(state="normal")
        self.audio_check.configure(state="disabled")
        self.toggle_button.configure(text="Parar compartilhamento")
        self._show_viewers()
        if self.session.audio_error:
            messagebox.showwarning(
                "ShareScreen",
                "O vídeo está sendo compartilhado, mas não foi possível capturar o áudio:\n"
                + self.session.audio_error,
            )

    def stop(self):
        self.session.close()
        self.session = None
        self.url = ""
        self.url_label.configure(text="")
        self.copy_button.configure(state="disabled")
        if pyaudio is not None:
            self.audio_check.configure(state="normal")
        self.toggle_button.configure(text="Iniciar compartilhamento")
        self.status.configure(text=IDLE_TEXT)

    def copy_url(self):
        self.clipboard_clear()
        self.clipboard_append(self.url)

    def close(self):
        if self.session:
            self.session.close()
        self.destroy()

    def _show_viewers(self):
        count = self.session.video.viewers
        viewers = f"{count} espectador{'' if count == 1 else 'es'}"
        self.status.configure(text=f"Compartilhando ({viewers}). Endereço para assistir:")

    def _tick(self):
        if self.session:
            self._show_viewers()
        self.after(500, self._tick)


def main():
    parser = argparse.ArgumentParser(description="Compartilha a tela ou uma janela, com áudio, pela rede local.")
    parser.add_argument("--port", type=int, default=8080, help="porta do servidor web (padrão: 8080)")
    App(parser.parse_args().port).mainloop()


if __name__ == "__main__":
    main()
