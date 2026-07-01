#!/usr/bin/env python3
"""
smaug_prep.py — prepare Smaug/Sauron audio for piper voice training.

Pipeline:
  1. Extract audio from video files (ffmpeg)
  2. Separate vocals from score (demucs)
  3. Transcribe + timestamp segments (faster-whisper)
  4. Segment into clips, filter by pitch (deep voice = Smaug)
  5. Output LJSpeech dataset ready for piper-train

Usage:
  # Step 1: prep a film file
  python smaug_prep.py prep "Hobbit.mkv" --out ./raw

  # Step 2: separate vocals (run once per film, slow)
  python smaug_prep.py separate ./raw --out ./separated

  # Step 3: transcribe + segment
  python smaug_prep.py segment ./separated --out ./segments

  # Step 4: interactive review (mark which clips are Smaug)
  python smaug_prep.py review ./segments

  # Step 5: build final dataset
  python smaug_prep.py dataset ./segments --out ./dataset

Dependencies:
  pip install demucs faster-whisper librosa soundfile
  (demucs needs torch — install CPU version if no GPU)
"""
import argparse
import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path


# ── Step 1: Extract audio ─────────────────────────────────────────────────────

def cmd_prep(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for src in args.inputs:
        src = Path(src)
        dst = out / (src.stem + ".wav")
        print(f"Extracting audio: {src.name} → {dst.name}")
        subprocess.run([
            "ffmpeg", "-y", "-i", str(src),
            "-vn", "-ar", "22050", "-ac", "1", "-c:a", "pcm_s16le",
            str(dst),
        ], check=True)
        print(f"  Done: {dst}")


# ── Step 2: Demucs vocal separation ──────────────────────────────────────────

def cmd_separate(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    wavs = list(Path(args.input).glob("*.wav"))
    if not wavs:
        print("No .wav files found in input directory.")
        sys.exit(1)
    for wav in wavs:
        print(f"Separating vocals: {wav.name}  (this takes a while)")
        subprocess.run([
            "python", "-m", "demucs",
            "--two-stems=vocals",
            "--out", str(out),
            str(wav),
        ], check=True)
        # demucs outputs to out/htdemucs/<stem>/vocals.wav
        vocals = out / "htdemucs" / wav.stem / "vocals.wav"
        if vocals.exists():
            dest = out / (wav.stem + "_vocals.wav")
            shutil.move(str(vocals), str(dest))
            print(f"  Vocals saved: {dest.name}")
        else:
            print(f"  Warning: vocals not found at expected path {vocals}")


# ── Step 3: Transcribe + segment ─────────────────────────────────────────────

def cmd_segment(args):
    try:
        from faster_whisper import WhisperModel
        import soundfile as sf
        import numpy as np
    except ImportError:
        print("Missing deps: pip install faster-whisper soundfile numpy")
        sys.exit(1)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    wavs_out = out / "wavs"
    wavs_out.mkdir(exist_ok=True)

    model = WhisperModel("medium.en", device="cpu", compute_type="int8")
    segments_meta = []
    clip_idx = 0

    for wav in sorted(Path(args.input).glob("*_vocals.wav")):
        print(f"Transcribing: {wav.name}")
        audio_data, sr = sf.read(str(wav), dtype="float32")

        segments, _ = model.transcribe(
            str(wav), language="en",
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            word_timestamps=False,
        )

        for seg in segments:
            duration = seg.end - seg.start
            # Skip very short or very long clips
            if duration < 1.5 or duration > 18.0:
                continue

            text = seg.text.strip()
            if not text or len(text) < 10:
                continue

            # Extract clip audio
            start_sample = int(seg.start * sr)
            end_sample   = int(seg.end   * sr)
            clip_audio   = audio_data[start_sample:end_sample]

            clip_name = f"smaug_{clip_idx:05d}"
            clip_path = wavs_out / f"{clip_name}.wav"
            sf.write(str(clip_path), clip_audio, sr)

            # Estimate fundamental frequency for Smaug filtering
            f0 = _estimate_f0(clip_audio, sr)

            segments_meta.append({
                "id":       clip_name,
                "text":     text,
                "source":   wav.stem,
                "start":    round(seg.start, 2),
                "end":      round(seg.end,   2),
                "duration": round(duration,  2),
                "f0_hz":    round(f0, 1),
                "smaug":    None,  # filled in during review
            })
            clip_idx += 1

    meta_path = out / "segments.json"
    with open(meta_path, "w") as f:
        json.dump(segments_meta, f, indent=2)
    print(f"\nSegmented {clip_idx} clips → {meta_path}")
    print(f"Smaug's voice is typically 80-160 Hz fundamental frequency.")
    print(f"Run: python smaug_prep.py review {out}")


def _estimate_f0(audio: "np.ndarray", sr: int) -> float:
    """Rough median fundamental frequency via autocorrelation."""
    try:
        import librosa
        f0, _, _ = librosa.pyin(audio, fmin=60, fmax=400, sr=sr)
        import numpy as np
        voiced = f0[~np.isnan(f0)]
        return float(np.median(voiced)) if len(voiced) > 0 else 0.0
    except Exception:
        return 0.0


# ── Step 4: Interactive review ────────────────────────────────────────────────

def cmd_review(args):
    meta_path = Path(args.input) / "segments.json"
    if not meta_path.exists():
        print(f"segments.json not found in {args.input}")
        sys.exit(1)

    with open(meta_path) as f:
        segments = json.load(f)

    unreviewed = [s for s in segments if s["smaug"] is None]
    print(f"\n{len(unreviewed)} clips to review ({len(segments)} total)")
    print("Controls: y=Smaug  n=skip  p=play  q=quit\n")

    for seg in unreviewed:
        clip_path = Path(args.input) / "wavs" / f"{seg['id']}.wav"
        print(f"[{seg['id']}] f0={seg['f0_hz']}Hz  {seg['duration']}s")
        print(f"  \"{seg['text']}\"")

        while True:
            choice = input("  > ").strip().lower()
            if choice == "p":
                subprocess.Popen(["aplay", "-q", str(clip_path)])
                continue
            elif choice == "y":
                seg["smaug"] = True
                break
            elif choice == "n":
                seg["smaug"] = False
                break
            elif choice == "q":
                break
            else:
                print("  y / n / p / q")
        else:
            continue
        break

    with open(meta_path, "w") as f:
        json.dump(segments, f, indent=2)

    approved = sum(1 for s in segments if s["smaug"] is True)
    total_dur = sum(s["duration"] for s in segments if s["smaug"] is True)
    print(f"\nApproved: {approved} clips, {total_dur/60:.1f} minutes")
    print(f"Piper needs ~30+ minutes for transfer learning.")


# ── Step 5: Build dataset ─────────────────────────────────────────────────────

def cmd_dataset(args):
    meta_path = Path(args.input) / "segments.json"
    with open(meta_path) as f:
        segments = json.load(f)

    approved = [s for s in segments if s["smaug"] is True]
    if not approved:
        print("No approved clips — run review first.")
        sys.exit(1)

    out = Path(args.out)
    wavs_out = out / "wavs"
    wavs_out.mkdir(parents=True, exist_ok=True)

    rows = []
    for seg in approved:
        src = Path(args.input) / "wavs" / f"{seg['id']}.wav"
        dst = wavs_out / f"{seg['id']}.wav"
        shutil.copy(str(src), str(dst))
        rows.append((seg["id"], seg["text"]))

    with open(out / "metadata.csv", "w", newline="") as f:
        writer = csv.writer(f, delimiter="|")
        for clip_id, text in rows:
            writer.writerow([clip_id, text])

    total_dur = sum(s["duration"] for s in approved)
    print(f"\nDataset ready: {len(approved)} clips, {total_dur/60:.1f} minutes")
    print(f"Location: {out}")
    print(f"\nNext steps:")
    print(f"  1. git clone https://github.com/rhasspy/piper")
    print(f"  2. Follow piper/TRAINING.md — use en_US-ryan-high as base model")
    print(f"  3. Point dataset_path to {out.resolve()}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Smaug voice dataset prep")
    sub = p.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("prep", help="Extract audio from video files")
    p1.add_argument("inputs", nargs="+", help="Video files (.mkv, .mp4, etc.)")
    p1.add_argument("--out", default="./raw")

    p2 = sub.add_parser("separate", help="Demucs vocal separation")
    p2.add_argument("input", help="Directory of extracted .wav files")
    p2.add_argument("--out", default="./separated")

    p3 = sub.add_parser("segment", help="Transcribe and segment into clips")
    p3.add_argument("input", help="Directory with *_vocals.wav files")
    p3.add_argument("--out", default="./segments")

    p4 = sub.add_parser("review", help="Interactively mark Smaug clips")
    p4.add_argument("input", help="Segments directory")

    p5 = sub.add_parser("dataset", help="Build final piper dataset")
    p5.add_argument("input", help="Segments directory")
    p5.add_argument("--out", default="./dataset")

    args = p.parse_args()
    {"prep": cmd_prep, "separate": cmd_separate, "segment": cmd_segment,
     "review": cmd_review, "dataset": cmd_dataset}[args.cmd](args)


if __name__ == "__main__":
    main()
