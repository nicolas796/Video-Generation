# Render Persistent Disk Setup

To prevent losing generated videos on every deploy, you need to set up a Render Disk.

## Why This is Needed

Render's filesystem is **ephemeral** — all files are wiped on every deploy. This means:
- Generated videos disappear after each code push
- Thumbnails are lost
- Uploads don't persist

A Render Disk provides **persistent storage** that survives deploys.

## Setup Instructions

### Step 1: Create a Disk in Render Dashboard

1. Go to [Render Dashboard](https://dashboard.render.com)
2. Click "Disks" in the left sidebar
3. Click "New Disk"
4. Configure:
   - **Name**: `video-uploads`
   - **Mount Path**: `/var/data`
   - **Size**: Start with 10 GB (you can resize later)
   - **Region**: Same as your web service
5. Click "Create"

### Step 2: Attach Disk to Your Service

1. Go to your web service in the Render dashboard
2. Click "Settings"
3. Scroll to "Disks" section
4. Select the `video-uploads` disk you just created
5. Set **Mount Path** to: `/var/data`
6. Click "Save Changes"

### Step 3: Set Environment Variable

1. In your service settings, go to "Environment" tab
2. Add a new environment variable:
   - **Key**: `RENDER_DISK_MOUNT_PATH`
   - **Value**: `/var/data`
3. Click "Save Changes"

### Step 4: Deploy

1. The disk will be mounted at `/var/data` on your next deploy
2. Videos will be saved to `/var/data/uploads/clips/`
3. Files will persist across deploys

## Verification

After deploy, check the logs for:
```
Upload folder: /var/data/uploads
Render Disk detected at /var/data
```

## Cost

Render Disks cost $0.25/GB per month:
- 10 GB = $2.50/month
- 20 GB = $5.00/month

You can resize the disk anytime in the dashboard.

## Alternative: AWS S3

For larger scale, consider using AWS S3 instead:
1. Create an S3 bucket
2. Update the code to upload/download from S3
3. More complex but unlimited storage

See `render.yaml` for disk configuration if using Infrastructure as Code.
