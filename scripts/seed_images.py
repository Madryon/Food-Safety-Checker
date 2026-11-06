"""
scripts/seed_images.py
Generates the royalty-free/CC0 image bank for the FoodCheck Custom CAPTCHA.
Creates 8 target categories with 42+ distinct 300x300 JPEG images each,
plus 42 distinct negative/distractor images (378 images total).
"""

import os
import sys
import math
import random

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw, ImageFilter
from captcha_config import IMAGE_BANK_DIR, CATEGORIES, NEGATIVES_CATEGORY, IMAGE_SIZE

def ensure_dirs():
    os.makedirs(IMAGE_BANK_DIR, exist_ok=True)
    for cat in list(CATEGORIES.keys()) + [NEGATIVES_CATEGORY]:
        os.makedirs(os.path.join(IMAGE_BANK_DIR, cat), exist_ok=True)

def draw_sky_and_ground(draw, width, height, seed, is_night=False):
    rng = random.Random(seed)
    if is_night:
        sky_top = (10, 15, 30)
        sky_bottom = (30, 40, 65)
        ground_color = (25, 28, 35)
    else:
        sky_palettes = [
            ((90, 160, 230), (190, 220, 250)),
            ((120, 180, 240), (220, 235, 250)),
            ((240, 150, 100), (250, 210, 170)), # sunset
            ((140, 160, 180), (210, 220, 230)), # overcast
        ]
        sky_top, sky_bottom = rng.choice(sky_palettes)
        ground_palettes = [
            (60, 65, 70), (75, 80, 85), (50, 55, 60), (45, 75, 45)
        ]
        ground_color = rng.choice(ground_palettes)

    # Gradient sky
    horizon = int(height * rng.uniform(0.5, 0.65))
    for y in range(horizon):
        t = y / max(1, horizon)
        r = int(sky_top[0] * (1 - t) + sky_bottom[0] * t)
        g = int(sky_top[1] * (1 - t) + sky_bottom[1] * t)
        b = int(sky_top[2] * (1 - t) + sky_bottom[2] * t)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    # Ground / road
    draw.rectangle([0, horizon, width, height], fill=ground_color)

    # Road perspective lines if asphalt
    if ground_color[0] < 90 and ground_color[1] < 90:
        curb_y = horizon + 10
        draw.line([0, height - 10, width, height - 10], fill=(90, 95, 100), width=2)
        # road divider dashed line
        center_x = width // 2
        for seg in range(horizon + 20, height, 25):
            draw.line([(center_x, seg), (center_x, min(height, seg + 12))], fill=(220, 210, 80), width=2)

    return horizon

# ── 1. TRAFFIC LIGHTS ────────────────────────────────────────────────────────
def generate_traffic_light(idx):
    img = Image.new("RGB", IMAGE_SIZE)
    draw = ImageDraw.Draw(img)
    rng = random.Random(idx * 733 + 11)
    is_night = (idx % 4 == 0)
    horizon = draw_sky_and_ground(draw, 300, 300, idx, is_night)

    # Pole placement
    pole_x = rng.randint(90, 210)
    pole_width = rng.randint(8, 14)
    draw.rectangle([pole_x - pole_width//2, 40, pole_x + pole_width//2, 300], fill=(45, 48, 52))

    # Housing
    hw = rng.randint(46, 56)
    hh = rng.randint(120, 145)
    hy = rng.randint(35, 70)
    hx = pole_x - hw // 2
    # Shadow and casing
    draw.rounded_rectangle([hx - 3, hy - 3, hx + hw + 3, hy + hh + 3], radius=8, fill=(15, 15, 18))
    draw.rounded_rectangle([hx, hy, hx + hw, hy + hh], radius=6, fill=(30, 32, 36), outline=(60, 65, 70), width=2)

    # Visors & 3 Lights: Red, Yellow, Green
    lamp_r = (hw - 16) // 2
    lamp_centers = [
        (hx + hw // 2, hy + hh * 0.2),
        (hx + hw // 2, hy + hh * 0.5),
        (hx + hw // 2, hy + hh * 0.8),
    ]

    active_light = idx % 3  # 0=red, 1=yellow, 2=green
    colors_lit = [(255, 45, 45), (255, 195, 25), (40, 235, 90)]
    colors_dim = [(90, 20, 20), (95, 75, 15), (15, 80, 35)]

    for i, (lx, ly) in enumerate(lamp_centers):
        # Visor arch
        draw.arc([lx - lamp_r - 2, ly - lamp_r - 6, lx + lamp_r + 2, ly + lamp_r], start=180, end=360, fill=(15, 15, 18), width=3)
        # Lens
        lit = (i == active_light)
        col = colors_lit[i] if lit else colors_dim[i]
        draw.ellipse([lx - lamp_r, ly - lamp_r, lx + lamp_r, ly + lamp_r], fill=col, outline=(10, 10, 12), width=1)
        if lit:
            # Highlight center
            draw.ellipse([lx - lamp_r * 0.4, ly - lamp_r * 0.4, lx + lamp_r * 0.4, ly + lamp_r * 0.4], fill=(255, 255, 255, 180))

    return img

# ── 2. BICYCLES ─────────────────────────────────────────────────────────────
def generate_bicycle(idx):
    img = Image.new("RGB", IMAGE_SIZE)
    draw = ImageDraw.Draw(img)
    rng = random.Random(idx * 541 + 43)
    horizon = draw_sky_and_ground(draw, 300, 300, idx)

    frame_colors = [
        (220, 40, 40), (30, 130, 230), (40, 180, 80), (240, 180, 20),
        (230, 230, 230), (35, 35, 40), (200, 80, 190), (255, 120, 20)
    ]
    frame_col = rng.choice(frame_colors)

    base_y = rng.randint(200, 230)
    wheel_r = rng.randint(38, 48)
    rear_x = rng.randint(60, 95)
    front_x = rear_x + rng.randint(110, 140)

    # Wheels (tires + rims + spokes)
    for wx in (rear_x, front_x):
        # Tire
        draw.ellipse([wx - wheel_r, base_y - wheel_r, wx + wheel_r, base_y + wheel_r], outline=(30, 32, 35), width=6)
        # Rim
        draw.ellipse([wx - wheel_r + 4, base_y - wheel_r + 4, wx + wheel_r - 4, base_y + wheel_r - 4], outline=(180, 185, 190), width=2)
        # Spokes
        for deg in range(0, 360, 45):
            rad = math.radians(deg)
            sx = wx + (wheel_r - 6) * math.cos(rad)
            sy = base_y + (wheel_r - 6) * math.sin(rad)
            draw.line([(wx, base_y), (sx, sy)], fill=(170, 175, 180), width=1)
        # Hub
        draw.ellipse([wx - 4, base_y - 4, wx + 4, base_y + 4], fill=(60, 65, 70))

    # Bottom bracket (pedal center)
    bb_x = (rear_x + front_x) // 2 - 10
    bb_y = base_y

    # Seat tube top
    seat_x = rear_x + (bb_x - rear_x) * 0.6
    seat_y = base_y - rng.randint(65, 80)

    # Head tube top
    head_x = front_x - 20
    head_y = base_y - rng.randint(70, 85)

    # Frame tubes
    tw = 4
    # Chainstay: rear hub to BB
    draw.line([(rear_x, base_y), (bb_x, bb_y)], fill=frame_col, width=tw)
    # Seatstay: rear hub to seat top
    draw.line([(rear_x, base_y), (seat_x, seat_y)], fill=frame_col, width=tw)
    # Seat tube: BB to seat top
    draw.line([(bb_x, bb_y), (seat_x, seat_y)], fill=frame_col, width=tw)
    # Down tube: BB to head tube
    draw.line([(bb_x, bb_y), (head_x, head_y)], fill=frame_col, width=tw)
    # Top tube: seat top to head tube
    draw.line([(seat_x, seat_y), (head_x, head_y)], fill=frame_col, width=tw)
    # Fork: front hub to head tube
    draw.line([(front_x, base_y), (head_x, head_y)], fill=(120, 125, 130), width=tw)

    # Saddle
    draw.polygon([(seat_x - 14, seat_y - 8), (seat_x + 14, seat_y - 8), (seat_x + 8, seat_y - 2), (seat_x - 10, seat_y - 2)], fill=(20, 20, 25))

    # Handlebars
    draw.line([(head_x, head_y), (head_x, head_y - 10)], fill=(120, 125, 130), width=3)
    draw.line([(head_x - 8, head_y - 12), (head_x + 8, head_y - 10)], fill=(30, 30, 35), width=4)

    return img

# ── 3. CARS ─────────────────────────────────────────────────────────────────
def generate_car(idx):
    img = Image.new("RGB", IMAGE_SIZE)
    draw = ImageDraw.Draw(img)
    rng = random.Random(idx * 317 + 89)
    horizon = draw_sky_and_ground(draw, 300, 300, idx)

    car_colors = [
        (210, 35, 35), (35, 95, 210), (235, 235, 240), (25, 28, 32),
        (230, 175, 20), (120, 125, 130), (30, 140, 120), (130, 40, 140)
    ]
    color = rng.choice(car_colors)

    cy = rng.randint(180, 215)
    cx = 150
    cw = rng.randint(180, 220)
    ch = rng.randint(55, 70)
    cabin_h = rng.randint(35, 45)

    # Shadow
    draw.ellipse([cx - cw//2 - 5, cy + ch - 5, cx + cw//2 + 5, cy + ch + 15], fill=(20, 22, 25, 160))

    # Main body
    draw.rounded_rectangle([cx - cw//2, cy, cx + cw//2, cy + ch], radius=12, fill=color, outline=(20, 20, 25), width=2)

    # Cabin / Roof
    cabin_w = int(cw * rng.uniform(0.55, 0.68))
    draw.rounded_rectangle([cx - cabin_w//2, cy - cabin_h + 10, cx + cabin_w//2, cy + 10], radius=14, fill=color)

    # Windows (front, rear, side)
    win_w = cabin_w - 14
    win_h = cabin_h - 10
    draw.rounded_rectangle([cx - win_w//2, cy - cabin_h + 12, cx + win_w//2, cy + 6], radius=6, fill=(160, 205, 235), outline=(40, 45, 50), width=2)
    # Window pillar
    draw.line([(cx, cy - cabin_h + 12), (cx, cy + 6)], fill=color, width=4)

    # Headlights & Taillights
    draw.rounded_rectangle([cx + cw//2 - 14, cy + 12, cx + cw//2 - 2, cy + 24], radius=3, fill=(255, 245, 180), outline=(180, 170, 80))
    draw.rounded_rectangle([cx - cw//2 + 2, cy + 12, cx - cw//2 + 12, cy + 24], radius=3, fill=(220, 30, 30), outline=(100, 10, 10))

    # Wheels & Hubcaps
    wr = 20
    for wx in (cx - cw//3, cx + cw//3):
        draw.ellipse([wx - wr, cy + ch - wr + 4, wx + wr, cy + ch + wr + 4], fill=(25, 26, 28), outline=(60, 62, 65), width=2)
        draw.ellipse([wx - wr//2, cy + ch - wr//2 + 4, wx + wr//2, cy + ch + wr//2 + 4], fill=(190, 195, 200), outline=(90, 95, 100))

    return img

# ── 4. FIRE HYDRANTS ────────────────────────────────────────────────────────
def generate_fire_hydrant(idx):
    img = Image.new("RGB", IMAGE_SIZE)
    draw = ImageDraw.Draw(img)
    rng = random.Random(idx * 643 + 29)
    # Sidewalk scene
    draw.rectangle([0, 0, 300, 170], fill=(150, 160, 170)) # Wall or street backdrop
    draw.rectangle([0, 170, 300, 300], fill=(110, 115, 120)) # Concrete sidewalk
    draw.line([0, 230, 300, 230], fill=(85, 90, 95), width=2) # Pavement seam

    hydrant_colors = [(225, 30, 30), (235, 180, 20), (200, 35, 45), (235, 225, 220)]
    h_col = rng.choice(hydrant_colors)

    hx = rng.randint(110, 190)
    hy = rng.randint(100, 140)
    body_w = rng.randint(46, 58)
    body_h = rng.randint(100, 125)

    # Shadow
    draw.ellipse([hx - body_w - 5, hy + body_h - 10, hx + body_w + 5, hy + body_h + 15], fill=(50, 52, 56))

    # Base flange
    draw.rounded_rectangle([hx - body_w//2 - 12, hy + body_h - 15, hx + body_w//2 + 12, hy + body_h + 5], radius=6, fill=(35, 35, 40))

    # Central body barrel
    draw.rectangle([hx - body_w//2, hy + 25, hx + body_w//2, hy + body_h], fill=h_col, outline=(30, 20, 20), width=2)

    # Side nozzles (left and right)
    nw = 16
    nh = 20
    draw.rectangle([hx - body_w//2 - nw, hy + 45, hx - body_w//2, hy + 45 + nh], fill=h_col, outline=(30, 20, 20), width=2)
    draw.ellipse([hx - body_w//2 - nw - 4, hy + 43, hx - body_w//2 - nw + 4, hy + 47 + nh], fill=(40, 42, 45))
    draw.rectangle([hx + body_w//2, hy + 45, hx + body_w//2 + nw, hy + 45 + nh], fill=h_col, outline=(30, 20, 20), width=2)
    draw.ellipse([hx + body_w//2 + nw - 4, hy + 43, hx + body_w//2 + nw + 4, hy + 47 + nh], fill=(40, 42, 45))

    # Front nozzle cap
    draw.ellipse([hx - 12, hy + 46, hx + 12, hy + 70], fill=(45, 48, 52), outline=h_col, width=3)

    # Bonnet / Top dome
    draw.ellipse([hx - body_w//2 - 4, hy + 8, hx + body_w//2 + 4, hy + 38], fill=h_col, outline=(30, 20, 20), width=2)

    # Operating top pentagon / nut
    draw.rectangle([hx - 8, hy - 4, hx + 8, hy + 12], fill=(40, 42, 45), outline=(15, 15, 18), width=1)

    return img

# ── 5. BRIDGES ──────────────────────────────────────────────────────────────
def generate_bridge(idx):
    img = Image.new("RGB", IMAGE_SIZE)
    draw = ImageDraw.Draw(img)
    rng = random.Random(idx * 883 + 97)

    # River & sky scene
    draw_sky_and_ground(draw, 300, 300, idx)
    # Water bottom
    draw.rectangle([0, 195, 300, 300], fill=(40, 95, 140))
    # Water waves/reflections
    for wy in range(205, 295, 14):
        draw.line([0, wy, 300, wy], fill=(60, 120, 165), width=2)

    bridge_style = idx % 3  # 0=suspension, 1=arch, 2=truss

    if bridge_style == 0:
        # Suspension bridge
        # Two main towers
        for tx in (70, 230):
            draw.rectangle([tx - 10, 50, tx + 10, 210], fill=(170, 45, 45))
            draw.rectangle([tx - 6, 80, tx + 6, 120], fill=(40, 95, 140))
            draw.rectangle([tx - 6, 135, tx + 6, 175], fill=(40, 95, 140))
        # Deck
        draw.rectangle([0, 175, 300, 188], fill=(50, 52, 55))
        # Main cables
        draw.arc([70, 50, 230, 290], start=180, end=360, fill=(30, 32, 35), width=3)
        draw.arc([-90, 50, 70, 290], start=270, end=360, fill=(30, 32, 35), width=3)
        draw.arc([230, 50, 390, 290], start=180, end=270, fill=(30, 32, 35), width=3)
        # Vertical suspenders
        for sx in range(85, 220, 12):
            draw.line([(sx, 175), (sx, 135)], fill=(120, 125, 130), width=1)

    elif bridge_style == 1:
        # Stone arch bridge
        draw.rectangle([0, 150, 300, 175], fill=(130, 125, 120))
        # Arches
        for ax in (60, 150, 240):
            draw.arc([ax - 35, 145, ax + 35, 215], start=180, end=360, fill=(40, 95, 140), width=14)
            draw.ellipse([ax - 30, 155, ax + 30, 215], fill=(40, 95, 140))
        # Railing
        for rx in range(10, 295, 15):
            draw.line([(rx, 140), (rx, 150)], fill=(90, 85, 80), width=2)
        draw.line([0, 140, 300, 140], fill=(80, 75, 70), width=3)

    else:
        # Steel truss bridge
        draw.rectangle([0, 165, 300, 180], fill=(60, 65, 70))
        # Piers
        draw.rectangle([55, 180, 80, 230], fill=(90, 92, 95))
        draw.rectangle([220, 180, 245, 230], fill=(90, 92, 95))
        # Truss triangles
        draw.line([0, 130, 300, 130], fill=(45, 100, 135), width=4)
        for tx in range(0, 300, 50):
            draw.line([(tx, 165), (tx + 25, 130)], fill=(45, 100, 135), width=3)
            draw.line([(tx + 25, 130), (tx + 50, 165)], fill=(45, 100, 135), width=3)
            draw.line([(tx, 165), (tx, 130)], fill=(45, 100, 135), width=2)

    return img

# ── 6. BUSES ────────────────────────────────────────────────────────────────
def generate_bus(idx):
    img = Image.new("RGB", IMAGE_SIZE)
    draw = ImageDraw.Draw(img)
    rng = random.Random(idx * 419 + 61)
    horizon = draw_sky_and_ground(draw, 300, 300, idx)

    bus_colors = [
        ((245, 185, 25), "School Bus"),
        ((35, 110, 215), "City Transit"),
        ((220, 45, 45), "Double Decker"),
        ((240, 240, 245), "Metro Express"),
        ((40, 160, 95), "Eco Transit")
    ]
    color, bus_type = rng.choice(bus_colors)

    bx = 150
    by = rng.randint(155, 180)
    bw = rng.randint(220, 250)
    bh = rng.randint(75, 95)

    # Shadow
    draw.ellipse([bx - bw//2 - 6, by + bh - 5, bx + bw//2 + 6, by + bh + 14], fill=(20, 22, 25))

    # Bus main rectangular body
    draw.rounded_rectangle([bx - bw//2, by, bx + bw//2, by + bh], radius=10, fill=color, outline=(25, 25, 30), width=2)

    # Windows band
    win_y = by + 12
    win_h = 24
    num_windows = 6
    w_width = (bw - 35) // num_windows

    for w_i in range(num_windows):
        wx = bx - bw//2 + 16 + w_i * w_width
        draw.rectangle([wx, win_y, wx + w_width - 6, win_y + win_h], fill=(175, 215, 240), outline=(35, 40, 45), width=1)

    # Destination display header
    draw.rectangle([bx - 35, by + 3, bx + 35, by + 9], fill=(20, 20, 25))
    draw.line([bx - 25, by + 6, bx + 25, by + 6], fill=(255, 190, 40), width=2)

    # Door outline
    door_x = bx + bw//2 - 38
    draw.rectangle([door_x, win_y, door_x + 18, by + bh - 4], fill=(190, 225, 245), outline=(40, 45, 50), width=2)

    # Headlights & Bumpers
    draw.rectangle([bx + bw//2 - 8, by + bh - 22, bx + bw//2 - 2, by + bh - 12], fill=(255, 245, 180))
    draw.rectangle([bx - bw//2 + 2, by + bh - 22, bx - bw//2 + 8, by + bh - 12], fill=(220, 30, 30))

    # Wheels
    wr = 18
    for wx in (bx - bw//3, bx + bw//3):
        draw.ellipse([wx - wr, by + bh - wr + 2, wx + wr, by + bh + wr + 2], fill=(25, 26, 28), outline=(60, 62, 65), width=2)
        draw.ellipse([wx - wr//2, by + bh - wr//2 + 2, wx + wr//2, by + bh + wr//2 + 2], fill=(190, 195, 200))

    return img

# ── 7. MOTORCYCLES ──────────────────────────────────────────────────────────
def generate_motorcycle(idx):
    img = Image.new("RGB", IMAGE_SIZE)
    draw = ImageDraw.Draw(img)
    rng = random.Random(idx * 709 + 37)
    horizon = draw_sky_and_ground(draw, 300, 300, idx)

    mc_colors = [(225, 30, 30), (35, 95, 220), (25, 28, 30), (240, 140, 20), (50, 180, 80)]
    color = rng.choice(mc_colors)

    base_y = rng.randint(205, 225)
    wr = 30
    rear_x = rng.randint(75, 95)
    front_x = rear_x + rng.randint(110, 130)

    # Wheels (spokes + rim + thick tire)
    for wx in (rear_x, front_x):
        draw.ellipse([wx - wr, base_y - wr, wx + wr, base_y + wr], outline=(25, 28, 32), width=7)
        draw.ellipse([wx - wr + 5, base_y - wr + 5, wx + wr - 5, base_y + wr - 5], outline=(170, 175, 180), width=2)
        for deg in (0, 60, 120, 180, 240, 300):
            rad = math.radians(deg)
            draw.line([(wx, base_y), (wx + (wr-6)*math.cos(rad), base_y + (wr-6)*math.sin(rad))], fill=(160, 165, 170), width=1)

    # Engine block & Transmission
    engine_x = (rear_x + front_x) // 2 - 10
    draw.rounded_rectangle([engine_x - 22, base_y - 25, engine_x + 22, base_y + 8], radius=4, fill=(60, 65, 70), outline=(20, 22, 25), width=2)
    # Cooling fins
    for fy in range(base_y - 20, base_y + 4, 5):
        draw.line([(engine_x - 18, fy), (engine_x + 18, fy)], fill=(120, 125, 130), width=2)

    # Exhaust pipe
    draw.line([(engine_x + 10, base_y), (rear_x - 15, base_y + 5)], fill=(200, 205, 210), width=5)

    # Fuel Tank & Seat
    tank_x = engine_x + 8
    tank_y = base_y - 45
    draw.ellipse([tank_x - 24, tank_y - 12, tank_x + 24, tank_y + 12], fill=color, outline=(20, 20, 25), width=2)
    # Seat
    seat_x = tank_x - 30
    draw.polygon([(seat_x - 20, tank_y + 2), (seat_x + 10, tank_y + 2), (seat_x + 6, tank_y + 12), (seat_x - 16, tank_y + 12)], fill=(20, 20, 22))

    # Front fork & Headlight
    head_x = front_x - 10
    head_y = base_y - 55
    draw.line([(front_x, base_y), (head_x, head_y)], fill=(180, 185, 190), width=4)
    # Headlight
    draw.ellipse([head_x + 4, head_y - 4, head_x + 18, head_y + 10], fill=(255, 245, 180), outline=(40, 42, 45), width=2)
    # Handlebar
    draw.line([(head_x - 6, head_y - 8), (head_x + 6, head_y - 12)], fill=(30, 32, 35), width=4)

    return img

# ── 8. CROSSWALKS ───────────────────────────────────────────────────────────
def generate_crosswalk(idx):
    img = Image.new("RGB", IMAGE_SIZE)
    draw = ImageDraw.Draw(img)
    rng = random.Random(idx * 521 + 79)

    # Top horizon scene
    draw_sky_and_ground(draw, 300, 300, idx)

    # Asphalt street surface
    road_top_y = 130
    draw.polygon([(0, 300), (300, 300), (260, road_top_y), (40, road_top_y)], fill=(45, 48, 52))

    # White zebra crossing bars with realistic perspective
    stripe_color = (245, 248, 250) if idx % 4 != 0 else (240, 210, 40) # standard white or yellow zebra
    num_stripes = rng.randint(6, 8)
    margin = 35

    for s_i in range(num_stripes):
        t1 = s_i / num_stripes
        t2 = (s_i + 0.65) / num_stripes

        # Bottom width
        bx1 = int(25 + t1 * 250)
        bx2 = int(25 + t2 * 250)
        # Top width (perspective converging)
        tx1 = int(65 + t1 * 170)
        tx2 = int(65 + t2 * 170)

        y_bot = rng.randint(265, 285)
        y_top = rng.randint(160, 185)

        draw.polygon([(bx1, y_bot), (bx2, y_bot), (tx2, y_top), (tx1, y_top)], fill=stripe_color)

    # Stop line or pedestrian walkway curb
    draw.line([(35, road_top_y + 40), (265, road_top_y + 40)], fill=(220, 225, 230), width=4)

    return img

# ── 9. NEGATIVES / DISTRACTORS ──────────────────────────────────────────────
def generate_negative(idx):
    img = Image.new("RGB", IMAGE_SIZE)
    draw = ImageDraw.Draw(img)
    rng = random.Random(idx * 947 + 13)

    kind = idx % 5
    if kind == 0:
        # Forest / Trees
        draw.rectangle([0, 0, 300, 160], fill=(135, 195, 245))
        draw.rectangle([0, 160, 300, 300], fill=(45, 110, 45))
        for tx in range(20, 290, 35):
            trunk_h = rng.randint(50, 90)
            draw.rectangle([tx - 4, 210 - trunk_h, tx + 4, 210], fill=(95, 60, 35))
            leaf_r = rng.randint(25, 40)
            draw.ellipse([tx - leaf_r, 210 - trunk_h - leaf_r, tx + leaf_r, 210 - trunk_h + leaf_r], fill=(30, 125, 40))

    elif kind == 1:
        # Brick wall texture
        draw.rectangle([0, 0, 300, 300], fill=(160, 65, 55))
        brick_h = 16
        brick_w = 40
        for y in range(0, 300, brick_h):
            draw.line([(0, y), (300, y)], fill=(210, 205, 195), width=2)
            offset = (brick_w // 2) if (y // brick_h) % 2 else 0
            for x in range(offset, 300, brick_w):
                draw.line([(x, y), (x, y + brick_h)], fill=(210, 205, 195), width=2)

    elif kind == 2:
        # Mountain landscape
        draw.rectangle([0, 0, 300, 170], fill=(245, 190, 140)) # sunset sky
        draw.ellipse([190, 40, 250, 100], fill=(255, 240, 180)) # sun
        draw.polygon([(0, 220), (100, 90), (210, 220)], fill=(75, 70, 95))
        draw.polygon([(110, 220), (220, 110), (300, 220)], fill=(95, 85, 115))
        draw.rectangle([0, 210, 300, 300], fill=(50, 75, 50))

    elif kind == 3:
        # Abstract geometric tiles
        tile_size = 30
        for y in range(0, 300, tile_size):
            for x in range(0, 300, tile_size):
                col = rng.choice([(45, 80, 120), (60, 110, 150), (90, 145, 180), (30, 55, 85)])
                draw.rectangle([x, y, x + tile_size - 1, y + tile_size - 1], fill=col)

    else:
        # Clouds and open sea / beach
        draw.rectangle([0, 0, 300, 180], fill=(120, 180, 240))
        # Clouds
        for cx in (60, 180, 250):
            draw.ellipse([cx - 35, 60, cx + 35, 95], fill=(245, 250, 255))
        # Ocean & Sand
        draw.rectangle([0, 180, 300, 240], fill=(30, 110, 160))
        draw.rectangle([0, 240, 300, 300], fill=(230, 205, 150))

    return img

GENERATORS = {
    "traffic_lights": generate_traffic_light,
    "bicycles": generate_bicycle,
    "cars": generate_car,
    "fire_hydrants": generate_fire_hydrant,
    "bridges": generate_bridge,
    "buses": generate_bus,
    "motorcycles": generate_motorcycle,
    "crosswalks": generate_crosswalk,
    NEGATIVES_CATEGORY: generate_negative,
}

def seed_image_bank(count_per_category=42):
    ensure_dirs()
    total = 0
    print(f"Generating image bank: {len(GENERATORS)} categories x {count_per_category} images...")
    for cat_name, gen_fn in GENERATORS.items():
        cat_dir = os.path.join(IMAGE_BANK_DIR, cat_name)
        for i in range(count_per_category):
            img = gen_fn(i + 1)
            # Ensure strictly 300x300
            if img.size != IMAGE_SIZE:
                img = img.resize(IMAGE_SIZE, Image.Resampling.LANCZOS)
            # Save as clean JPEG without EXIF
            filename = f"img_{i+1:03d}.jpg"
            out_path = os.path.join(cat_dir, filename)
            img.save(out_path, format="JPEG", quality=90, optimize=True)
            total += 1
        print(f"  [OK] Generated {count_per_category} images for category: {cat_name}")

    print(f"Finished! Total images created in {IMAGE_BANK_DIR}: {total}")

if __name__ == "__main__":
    seed_image_bank()
