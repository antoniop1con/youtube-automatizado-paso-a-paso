#!/usr/bin/env python3
"""
Prueba de humo: comprueba las cuatro piezas externas antes de gastar dinero.

    py test_api.py

Coste: unos 4 centimos (una imagen y una frase de voz).
"""
import os, pathlib, subprocess, sys

OK, MAL = "  OK   ", "  FALLA"
fallos = []


def titulo(n, t):
    print(f"\n{n}) {t}")


# ---------------------------------------------------------------- 1) claves
titulo(1, "Variables de entorno")
for nombre in ("GEMINI_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"):
    v = os.environ.get(nombre, "")
    if v:
        print(f"{OK} {nombre} = {v[:6]}... ({len(v)} caracteres)")
    else:
        print(f"{MAL} {nombre} no esta definida")
        fallos.append(nombre)
if fallos:
    print("\n  -> Cierra PowerShell, abrelo de nuevo y vuelve a probar.")
    sys.exit(1)

import pipeline as P

# ------------------------------------------------------------- 2) ffmpeg
titulo(2, "ffmpeg y ffprobe")
for cmd in ("ffmpeg", "ffprobe"):
    try:
        subprocess.run([cmd, "-version"], capture_output=True, check=True)
        print(f"{OK} {cmd} responde")
    except Exception as ex:
        print(f"{MAL} {cmd}: {ex}")
        fallos.append(cmd)

# -------------------------------------------------------- 3) referencias
titulo(3, "Carpeta de referencias")
refs = P.archivos_ref()
if refs:
    for r in refs:
        print(f"{OK} {r.name} ({r.stat().st_size//1024} KB)")
else:
    print(f"{MAL} vacia: el estilo variara entre planos")
    fallos.append("referencias")

# ------------------------------------------------------ 4) modelo texto
titulo(4, f"Modelo de texto ({P.MODELO_TEXTO})")
try:
    from google.genai import types
    r = P.cliente().models.generate_content(
        model=P.MODELO_TEXTO,
        contents='Devuelve SOLO este JSON: [{"id":1,"ok":true}]',
        config=types.GenerateContentConfig(response_mime_type="application/json"))
    print(f"{OK} responde -> {r.text.strip()[:60]}")
except Exception as ex:
    print(f"{MAL} {type(ex).__name__}: {ex}")
    fallos.append("modelo de texto")

# ----------------------------------------------------- 5) modelo imagen
titulo(5, f"Modelo de imagen ({P.MODELO_IMG})  ~0,03 EUR")
try:
    escena = {"id": 999, "plano": "escena", "rotulo": "PRUEBA",
              "prompt": "A stick figure standing on flat ground under a flat sky band."}
    P.IMGS.mkdir(parents=True, exist_ok=True)
    destino = P.generar_imagen(escena, log=lambda m: print("    ", m))
    print(f"{OK} imagen generada -> {destino} ({destino.stat().st_size//1024} KB)")
except Exception as ex:
    print(f"{MAL} {type(ex).__name__}: {ex}")
    fallos.append("modelo de imagen")

# ------------------------------------------------- 6) control automatico
titulo(6, "Control automatico de la imagen")
try:
    v = P.verificar_imagen(escena, log=lambda m: print("    ", m))
    print(f"{OK} texto_ok={v['texto_ok']}  estilo_ok={v['estilo_ok']}  {v.get('nota','')}")
    if not v["texto_ok"]:
        print("       (normal si el rotulo PRUEBA no salio bien; el pipeline lo reintenta)")
except Exception as ex:
    print(f"{MAL} {type(ex).__name__}: {ex}")
    fallos.append("control automatico")

# -------------------------------------------------------- 7) ElevenLabs
titulo(7, "ElevenLabs  ~0,01 EUR")
try:
    mp3 = P.generar_voz_frase(999, "Esto es una prueba de voz.", "eleven")
    dur = P._dur(mp3)
    print(f"{OK} audio generado -> {mp3} ({dur:.1f} s)")
except Exception as ex:
    print(f"{MAL} {type(ex).__name__}: {ex}")
    fallos.append("ElevenLabs")

# ------------------------------------------------------------ 8) montaje
titulo(8, "Montaje de un clip con ffmpeg")
try:
    esc = [{"id": 999, "frase_id": 999, "texto": "Esto es una prueba de voz.",
            "texto_frase": "Esto es una prueba de voz."}]
    P.montar_video(esc, forzar=True, log=lambda m: print("    ", m))
    print(f"{OK} video de prueba montado")
except Exception as ex:
    print(f"{MAL} {type(ex).__name__}: {ex}")
    fallos.append("montaje")

# ------------------------------------------------------------ resultado
print("\n" + "=" * 52)
if fallos:
    print("FALLAN:", ", ".join(fallos))
    print("No lances la generacion completa hasta arreglarlo.")
else:
    print("TODO CORRECTO. Puedes generar con seguridad.")
    print("Borra la carpeta out/ antes de empezar el video de verdad.")
print("=" * 52)