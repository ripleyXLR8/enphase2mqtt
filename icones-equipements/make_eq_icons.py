"""Quatre icones d'equipement pour la chaine Enphase, dans le style maison :
carre bleu profond arrondi, glyphe blanc, accents cyan.
Palette relevee sur electrolux2mqtt-icon.png.
"""
import os

from PIL import Image, ImageDraw

S = 512
SS = 4
BLEU = (14, 63, 92, 255)
BLANC = (244, 248, 250, 255)
CYAN = (41, 182, 246, 255)
OUT = os.path.dirname(os.path.abspath(__file__))


def neuf():
    im = Image.new("RGBA", (S * SS, S * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, S * SS - 1, S * SS - 1], radius=sc(110), fill=BLEU)
    return im, d


def sc(v):
    return int(v * SS)


def lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def panneau(d, hg, hd, bg, bb, cols=4, rows=3, ep=9):
    """Un panneau en perspective : quadrilatere blanc quadrille."""
    d.polygon([hg, hd, bb, bg], fill=BLANC)
    for i in range(1, cols):
        d.line([lerp(hg, hd, i / cols), lerp(bg, bb, i / cols)], fill=BLEU, width=sc(ep))
    for j in range(1, rows):
        d.line([lerp(hg, bg, j / rows), lerp(hd, bb, j / rows)], fill=BLEU, width=sc(ep))


# --- 1. La passerelle : un boitier, ses diodes, et les ondes MQTT ----------
im, d = neuf()
d.rounded_rectangle([sc(116), sc(178), sc(396), sc(334)], radius=sc(26), fill=BLANC)
for i, x in enumerate((160, 208, 256)):
    d.ellipse([sc(x), sc(212), sc(x + 30), sc(242)], fill=CYAN if i == 0 else BLEU)
d.rounded_rectangle([sc(160), sc(276), sc(352), sc(300)], radius=sc(12), fill=BLEU)
cx, cy = sc(330), sc(392)
d.ellipse([cx - sc(12), cy - sc(12), cx + sc(12), cy + sc(12)], fill=CYAN)
for r in (44, 78, 112):
    d.arc([cx - sc(r), cy - sc(r), cx + sc(r), cy + sc(r)], start=180, end=270, fill=CYAN, width=sc(14))
im.resize((S, S), Image.LANCZOS).save(os.path.join(OUT, "ENPHASE-ENVOY.png"))

# --- 2. Une phase : la sinusoide ------------------------------------------
im, d = neuf()
pts = []
import math
for k in range(0, 361):
    x = sc(96) + (sc(320) * k / 360.0)
    y = sc(256) - math.sin(math.radians(k * 2)) * sc(96)
    pts.append((x, y))
d.line(pts, fill=BLANC, width=sc(22), joint="curve")
d.line([sc(96), sc(256), sc(416), sc(256)], fill=CYAN, width=sc(8))
d.rounded_rectangle([sc(176), sc(370), sc(336), sc(410)], radius=sc(14), fill=CYAN)
im.resize((S, S), Image.LANCZOS).save(os.path.join(OUT, "ENPHASE-PHASE.png"))

# --- 3. Un panneau seul ----------------------------------------------------
im, d = neuf()
panneau(d, (sc(132), sc(130)), (sc(380), sc(130)), (sc(96), sc(342)), (sc(416), sc(342)))
d.polygon([(sc(240), sc(342)), (sc(272), sc(342)), (sc(282), sc(414)), (sc(230), sc(414))], fill=BLANC)
d.rounded_rectangle([sc(186), sc(408), sc(326), sc(432)], radius=sc(12), fill=BLANC)
im.resize((S, S), Image.LANCZOS).save(os.path.join(OUT, "ENPHASE-PANEL.png"))

# --- 4. Un champ : trois panneaux cote a cote ------------------------------
im, d = neuf()
for i, dx in enumerate((-146, 0, 146)):
    panneau(d,
            (sc(196 + dx), sc(150)), (sc(316 + dx), sc(150)),
            (sc(180 + dx), sc(300)), (sc(332 + dx), sc(300)),
            cols=2, rows=2, ep=8)
d.rounded_rectangle([sc(96), sc(330), sc(416), sc(356)], radius=sc(13), fill=CYAN)
d.rounded_rectangle([sc(150), sc(380), sc(362), sc(404)], radius=sc(12), fill=BLANC)
im.resize((S, S), Image.LANCZOS).save(os.path.join(OUT, "ENPHASE-ARRAY.png"))

print("4 icones ecrites dans", OUT)
