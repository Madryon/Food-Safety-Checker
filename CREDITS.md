# CAPTCHA Assets & Credits

## Overview
All images and audio assets generated and used by the FoodCheck Custom CAPTCHA system are strictly royalty-free and dedicated to the public domain under the Creative Commons CC0 1.0 Universal Public Domain Dedication.

## Image Bank
- **Total Images**: 378 images (300x300 JPEG)
- **Categories (8 target + 1 negative)**:
  - `traffic_lights`: 42 images (simulated urban scenes, signals, and luminaries)
  - `bicycles`: 42 images (various frame geometries, wheels, spokes, pedals, saddles)
  - `cars`: 42 images (sedans, coupes, SUVs with lighting and wheels)
  - `fire_hydrants`: 42 images (cast-iron hydrants with nozzles, valves, sidewalk backdrops)
  - `bridges`: 42 images (suspension, stone arch, and truss bridges over water)
  - `buses`: 42 images (city transit, commuter coaches, school buses)
  - `motorcycles`: 42 images (cruisers and sport bikes with engines, forks, exhausts)
  - `crosswalks`: 42 images (perspective zebra crossings on asphalt lanes)
  - `negatives`: 42 images (forests, brick textures, mountains, tile patterns, cloudscapes)
- **Generation Source**: Procedural vector and canvas rendering generated via `scripts/seed_images.py`.
- **License**: Creative Commons CC0 1.0 Universal (Public Domain).
- **Processing**: EXIF metadata stripped; augmented with random subtle cropping, brightness, contrast, and noise per request to defeat static perceptual hashing.

## Audio Bank
- **Sound Generation**: Pure sinusoidal multi-frequency DTMF and acoustic audio waveforms generated programmatically in `services/captcha/audio.py` using Python's standard `wave` library.
- **License**: Creative Commons CC0 1.0 Universal (Public Domain).
