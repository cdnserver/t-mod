from pathlib import Path
import math
import random
import subprocess
import wave
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "davinci_sync_demo"
SOURCE = ROOT / "artifacts" / "t-mod-gource-1080p60.mp4"
MUSIC = OUT / "sync_demo_music.wav"
XML = OUT / "Beat_Sync_Demo.fcpxml"
PREVIEW = OUT / "Beat_Sync_Demo_preview.mp4"

FPS = 60
SAMPLE_RATE = 48_000
BPM = 120
BEAT = 60.0 / BPM
DURATION = 24.0
SOURCE_DURATION = 32.366667


def make_music() -> None:
    """Create a small original 120 BPM demo track with obvious beat accents."""
    OUT.mkdir(parents=True, exist_ok=True)
    total = int(DURATION * SAMPLE_RATE)
    rng = random.Random(314159)
    chord_sets = [
        (220.00, 261.63, 329.63),  # Am
        (174.61, 220.00, 261.63),  # F
        (130.81, 164.81, 196.00),  # C
        (196.00, 246.94, 293.66),  # G
    ]
    roots = [110.00, 87.31, 65.41, 98.00]
    previous_noise = 0.0

    with wave.open(str(MUSIC), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        frames = bytearray()
        for i in range(total):
            t = i / SAMPLE_RATE
            beat_index = int(t / BEAT)
            beat_phase = t - beat_index * BEAT
            chord = chord_sets[int(t // 2.0) % len(chord_sets)]
            root = roots[int(t // 2.0) % len(roots)]

            # Soft pad and bass keep the demo musical without being distracting.
            pad = sum(math.sin(2 * math.pi * f * t) for f in chord) / 3.0
            pad *= 0.10 * (0.82 + 0.18 * math.sin(math.pi * (t % 2.0) / 2.0))
            bass = math.sin(2 * math.pi * root * t) * 0.075

            # Kick on every beat, snare on beats 2 and 4.
            kick_env = math.exp(-18.0 * beat_phase)
            kick = math.sin(2 * math.pi * (92.0 - 34.0 * beat_phase / BEAT) * beat_phase)
            kick *= 0.34 * kick_env if beat_phase < 0.24 else 0.0

            snare_phase = t % 1.0
            snare_env = math.exp(-22.0 * snare_phase)
            noise = rng.uniform(-1.0, 1.0)
            snare_noise = (noise - previous_noise) * 0.17 * snare_env if snare_phase < 0.20 else 0.0
            previous_noise = noise

            # Short eighth-note hats give the cut points a clear pulse.
            hat_phase = t % 0.25
            hat_env = math.exp(-38.0 * hat_phase)
            hat = rng.uniform(-1.0, 1.0) * 0.045 * hat_env if hat_phase < 0.11 else 0.0

            # A gentle pluck at the start of each two-second phrase.
            phrase_phase = t % 2.0
            pluck_env = math.exp(-3.8 * phrase_phase)
            pluck = math.sin(2 * math.pi * (chord[1] * 2.0) * phrase_phase) * 0.06 * pluck_env

            sample = max(-0.92, min(0.92, (pad + bass + kick + snare_noise + hat + pluck) * 1.45))
            # Tiny stereo spread.
            left = int(max(-1.0, min(1.0, sample * 0.98)) * 32767)
            right = int(max(-1.0, min(1.0, sample * 1.02)) * 32767)
            frames.extend(left.to_bytes(2, "little", signed=True))
            frames.extend(right.to_bytes(2, "little", signed=True))
        wav.writeframes(frames)


def source_offsets() -> list[float]:
    # Jump through the source at every beat so the edit is visibly cut to rhythm.
    return [round(((i * 0.67) % (SOURCE_DURATION - BEAT)) * FPS) / FPS for i in range(int(DURATION / BEAT))]


def make_fcpxml(offsets: list[float]) -> None:
    fcpxml = ET.Element("fcpxml", {"version": "1.10"})
    resources = ET.SubElement(fcpxml, "resources")
    ET.SubElement(
        resources,
        "format",
        {
            "id": "r1",
            "name": "FFVideoFormat1080p60",
            "frameDuration": "1/60s",
            "width": "1920",
            "height": "1080",
        },
    )
    ET.SubElement(
        resources,
        "asset",
        {
            "id": "r2",
            "name": "Gource source video",
            "src": SOURCE.as_uri(),
            "start": "0s",
            "duration": f"{SOURCE_DURATION:.6f}s",
            "hasVideo": "1",
            "hasAudio": "0",
            "format": "r1",
        },
    )
    ET.SubElement(
        resources,
        "asset",
        {
            "id": "r3",
            "name": "120 BPM sync demo music",
            "src": MUSIC.as_uri(),
            "start": "0s",
            "duration": f"{DURATION:.6f}s",
            "hasVideo": "0",
            "hasAudio": "1",
            "audioSources": "1",
            "audioChannels": "2",
            "audioRate": str(SAMPLE_RATE),
        },
    )

    library = ET.SubElement(fcpxml, "library", {"location": "file:///"})
    event = ET.SubElement(library, "event", {"name": "AI Sync Demo"})
    project = ET.SubElement(event, "project", {"name": "Beat Sync Demo"})
    sequence = ET.SubElement(
        project,
        "sequence",
        {"format": "r1", "duration": f"{DURATION:.6f}s", "tcStart": "0s", "tcFormat": "NDF"},
    )
    spine = ET.SubElement(sequence, "spine")

    for i, start in enumerate(offsets):
        clip = ET.SubElement(
            spine,
            "asset-clip",
            {
                "name": f"Beat cut {i + 1:02d}",
                "ref": "r2",
                "offset": f"{i * BEAT:.6f}s",
                "start": f"{start:.6f}s",
                "duration": f"{BEAT:.6f}s",
                "lane": "0",
            },
        )
        ET.SubElement(clip, "marker", {"start": "0s", "duration": "1/60s", "value": f"Beat {i + 1:02d}"})

    audio = ET.SubElement(
        spine,
        "asset-clip",
        {
            "name": "SYNC MUSIC — 120 BPM",
            "ref": "r3",
            "offset": "0s",
            "start": "0s",
            "duration": f"{DURATION:.6f}s",
            "lane": "-1",
        },
    )
    ET.SubElement(audio, "note", {"value": "Original demo music, 120 BPM. Cuts land on every beat."})

    tree = ET.ElementTree(fcpxml)
    try:
        ET.indent(tree, space="  ")
    except AttributeError:
        pass
    tree.write(XML, encoding="utf-8", xml_declaration=True)


def render_preview(offsets: list[float]) -> None:
    graph = []
    labels = []
    for i, start in enumerate(offsets):
        end = start + BEAT
        graph.append(f"[0:v]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS[v{i}]")
        labels.append(f"[v{i}]")
    graph.append("".join(labels) + f"concat=n={len(labels)}:v=1:a=0[outv]")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(SOURCE),
            "-i",
            str(MUSIC),
            "-filter_complex",
            ";".join(graph),
            "-map",
            "[outv]",
            "-map",
            "1:a:0",
            "-t",
            str(DURATION),
            "-r",
            str(FPS),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(PREVIEW),
        ],
        check=True,
    )


def write_beat_map(offsets: list[float]) -> None:
    lines = [
        "BEAT SYNC DEMO",
        "Tempo: 120 BPM | Beat length: 0.500 s | Timeline: 24.000 s",
        "",
        "Beat | Timeline | Source in t-mod-gource-1080p60.mp4",
    ]
    for i, start in enumerate(offsets, 1):
        lines.append(f"{i:>4} | {((i - 1) * BEAT):>8.3f}s | {start:>8.3f}s")
    (OUT / "BEAT_MAP.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    if not SOURCE.exists():
        raise FileNotFoundError(SOURCE)
    offsets = source_offsets()
    make_music()
    make_fcpxml(offsets)
    write_beat_map(offsets)
    render_preview(offsets)
    print(f"Created: {XML}")
    print(f"Created: {MUSIC}")
    print(f"Created: {PREVIEW}")


if __name__ == "__main__":
    main()
