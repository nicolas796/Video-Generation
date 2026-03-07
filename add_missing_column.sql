-- Add only the missing pollo_video_url column
ALTER TABLE video_clips ADD COLUMN IF NOT EXISTS pollo_video_url VARCHAR(1000);
