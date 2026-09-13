import unittest

from shadow_rb_serato.model import CuePoint, Track
from shadow_rb_serato.preview import preview


def make_track(source_id: str, path: str, size: int = 10, length_ms: int = 100_000) -> Track:
    return Track(
        source_id=source_id,
        title="Song",
        artist="Artist",
        path=path,
        file_name=path.rsplit("/", 1)[-1],
        size=size,
        length_ms=length_ms,
        bpm=128.0,
        key="8A",
        rating=0,
        color=0,
        cues=(CuePoint(1, 1000, None, "intro", 255, False, 0),),
    )


class PreviewTests(unittest.TestCase):
    def test_exact_path_match_preserves_cue_count(self) -> None:
        result = preview("rekordbox", [make_track("rb:1", "/music/song.mp3")], "serato", [make_track("s:1", "/music/song.mp3")])
        self.assertEqual(result["summary"]["exact_path"], 1)
        self.assertEqual(result["summary"]["matched_with_cues"], 1)
        self.assertEqual(result["matches"][0]["cue_count"], 1)

    def test_streaming_source_is_explicitly_unverifiable(self) -> None:
        source = make_track("rb:1", "apple-music:tracks:1")
        target = make_track("s:1", "streaming://1")
        object.__setattr__(target, "source_kind", "streaming")
        object.__setattr__(source, "source_kind", "streaming")
        result = preview("rekordbox", [source], "serato", [target])
        self.assertEqual(result["summary"]["unmatched"], 1)
        self.assertTrue(any("not locally verifiable" in warning for warning in result["matches"][0]["warnings"]))

    def test_duration_difference_lowers_confidence(self) -> None:
        result = preview("rekordbox", [make_track("rb:1", "/music/song.mp3")], "serato", [make_track("s:1", "/music/song.mp3", length_ms=104_000)])
        self.assertEqual(result["matches"][0]["confidence"], 0.45)
        self.assertIn("duration differs", result["matches"][0]["warnings"][0])


if __name__ == "__main__":
    unittest.main()
