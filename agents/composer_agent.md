# ComposerAgent

## Role
Assemble the final newscast video from all generated assets.

## Input
- job_id: Job identifier
- script: Generated script dict
- scroll_video: Path to scroll video (may be null)
- extracted_videos: List of downloaded video dicts
- audio_sections: Dict of narration audio paths

## Steps
1. Call `compose_video` with all inputs
2. Verify the output file exists and report its path and duration
3. If composition fails, report the specific error stage

## Notes
- Video must be at 1920x1080, H.264/AAC, MP4
- Output goes to ~/newscast-ai/output/
