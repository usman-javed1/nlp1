import os
import re
import time
import json
import threading
import logging
import requests
import subprocess
import tempfile
from pytube import Playlist
import concurrent.futures
import boto3
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configuration
MAX_RETRY_ATTEMPTS = 5
REQUEST_DELAY = 2
TEMP_DIR = tempfile.gettempdir()  # Use system temp directory
TRANSCRIPT_DIR = "transcripts"  # Only for finding transcripts, not storing
MAX_THREADS = 4
INSTANCE_ID = os.environ.get("AWS_INSTANCE_ID", f"worker-{threading.get_native_id()}")

# AWS S3 credentials from .env file
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")  # Default to us-east-1 if not specified
S3_BUCKET = os.environ.get("S3_BUCKET")
S3_COORD_BUCKET = os.environ.get("S3_COORD_BUCKET")

# Import drama data from transcript_fetcher
try:
    from transcript_fetcher import dramas, url_to_id
except ImportError:
    print("ERROR: Failed to import data from transcript_fetcher.py")
    raise

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("video_downloader.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("video_downloader")

class S3Uploader:
    def __init__(self):
        """Initialize S3 client using AWS credentials"""
        print("Initializing S3 uploader...")
        
        if not AWS_ACCESS_KEY_ID or not AWS_SECRET_ACCESS_KEY or not S3_BUCKET:
            raise Exception("AWS credentials or bucket name missing from .env file")
            
        try:
            # Initialize the S3 client
            self.s3_client = boto3.client(
                's3',
                aws_access_key_id=AWS_ACCESS_KEY_ID,
                aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
                region_name=AWS_REGION
            )
            
            # Test connection by listing buckets
            self.s3_client.list_buckets()
            print(f"✓ Successfully connected to AWS S3")
            print(f"✓ Using bucket: {S3_BUCKET}")
            
            # Verify bucket exists
            self.s3_client.head_bucket(Bucket=S3_BUCKET)
            print(f"✓ Bucket {S3_BUCKET} verified")
            
        except Exception as e:
            logger.error(f"S3 initialization error: {str(e)}")
            raise Exception(f"Failed to initialize S3: {str(e)}")
    
    def upload_file(self, local_path, remote_path):
        """Upload a file to S3 and return the URL"""
        try:
            # Remove leading slash for S3 keys
            s3_key = remote_path.lstrip('/')
            
            print(f"Uploading file to S3: {local_path} → s3://{S3_BUCKET}/{s3_key}")
            file_size = os.path.getsize(local_path) / (1024 * 1024)
            print(f"File size: {file_size:.2f} MB")
            
            # Upload the file to S3
            self.s3_client.upload_file(
                local_path, 
                S3_BUCKET, 
                s3_key,
                ExtraArgs={'ACL': 'public-read'}  # Make object publicly accessible
            )
            
            # Generate the S3 URL
            s3_url = f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"
            print(f"✓ Successfully uploaded file to S3: {s3_url}")
            
            return s3_url
        except Exception as e:
            logger.error(f"S3 upload error: {str(e)}")
            raise Exception(f"Failed to upload to S3: {str(e)}")

class VideoDownloader:
    def __init__(self):
        print("\n" + "*"*60)
        print(f"DRAMA VIDEO DOWNLOADER (Version 1.5)")
        print(f"Started at: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Running on instance: {INSTANCE_ID}")
        print(f"Temp directory: {TEMP_DIR}")
        print("*"*60 + "\n")
        
        # Check for yt-dlp
        self.yt_dlp_available = False
        try:
            result = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True)
            if result.returncode == 0:
                self.yt_dlp_available = True
                print(f"Found yt-dlp version: {result.stdout.strip()}")
            else:
                print("yt-dlp not found. Will use fallback methods.")
        except:
            print("yt-dlp not found. Will use fallback methods.")
        
        # Initialize S3 uploader - will fail if credentials are missing
        self.s3 = S3Uploader()
        
        # Initialize set to keep track of processed episodes
        self.processed_episodes = set()
    
    def download_video(self, url, output_path):
        """Download a video using yt-dlp or fallback methods"""
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        # Try yt-dlp first (most reliable)
        if self.yt_dlp_available:
            try:
                print(f"Downloading video using yt-dlp: {url}")
                cmd = [
                    "yt-dlp",
                    "-f", "best",
                    "-o", output_path,
                    "--no-playlist",
                    "--no-progress",
                    "--quiet",
                    url
                ]
                
                result = subprocess.run(cmd, capture_output=True, text=True)
                
                if result.returncode == 0:
                    if os.path.exists(output_path):
                        print(f"✓ Successfully downloaded video using yt-dlp")
                        return output_path
                    else:
                        print(f"⚠ yt-dlp claimed success but file not found")
                else:
                    print(f"✗ yt-dlp download failed: {result.stderr}")
            except Exception as e:
                print(f"✗ yt-dlp error: {str(e)}")
        
        # Fallback 1: Try using pytube directly
        try:
            from pytube import YouTube
            print(f"Falling back to pytube for download: {url}")
            
            yt = YouTube(url)
            video = yt.streams.get_highest_resolution()
            downloaded_path = video.download(output_path=os.path.dirname(output_path))
            
            # Rename if necessary to match expected output path
            if downloaded_path != output_path and os.path.exists(downloaded_path):
                os.rename(downloaded_path, output_path)
            
            if os.path.exists(output_path):
                print(f"✓ Successfully downloaded video using pytube")
                return output_path
            else:
                print(f"⚠ pytube claimed success but file not found")
        except Exception as e:
            print(f"✗ pytube download error: {str(e)}")
        
        # Fallback 2: Try using requests
        try:
            print(f"Last resort: Trying direct download via requests: {url}")
            response = requests.get(url, stream=True)
            if response.status_code == 200:
                with open(output_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                
                if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                    print(f"✓ Successfully downloaded video using requests")
                    return output_path
                else:
                    print(f"⚠ requests download succeeded but file is empty")
            else:
                print(f"✗ requests download failed with status code: {response.status_code}")
        except Exception as e:
            print(f"✗ requests download error: {str(e)}")
        
        print(f"All download methods failed for URL: {url}")
        return None
    
    def process_episode(self, drama_name, idx, url):
        """Process a single episode: download and upload to S3"""
        episode_key = f"{drama_name}_ep{idx}"
        
        # Check if already processed
        if episode_key in self.processed_episodes:
            print(f"⚠ Episode {idx} already processed. Skipping.")
            return True
        
        print(f"Processing {drama_name} - Episode {idx}")
        print(f"Video URL: {url}")
        
        # Create temporary directory for this episode
        episode_dir = os.path.join(TEMP_DIR, f"drama_{int(time.time())}_{threading.get_native_id()}")
        os.makedirs(episode_dir, exist_ok=True)
        
        try:
            # Set output filename
            video_id = url_to_id(url)
            output_filename = f"{drama_name}_Ep{idx}_{video_id}.mp4"
            output_path = os.path.join(episode_dir, output_filename)
            
            # Download the video
            downloaded_path = self.download_video(url, output_path)
            
            if downloaded_path:
                # Calculate file size
                file_size = os.path.getsize(downloaded_path) / (1024 * 1024)  # Size in MB
                print(f"Downloaded video size: {file_size:.2f} MB")
                
                # Upload to S3
                remote_path = f"/videos/{drama_name}/{output_filename}"
                s3_url = self.s3.upload_file(downloaded_path, remote_path)
                
                if s3_url:
                    print(f"✓ Video uploaded to S3: {s3_url}")
                else:
                    print(f"✗ Failed to upload video to S3")
                    return False
                
                # Delete temporary file to save space
                try:
                    os.remove(downloaded_path)
                    print(f"✓ Removed temporary file: {downloaded_path}")
                except Exception as e:
                    print(f"⚠ Error removing temporary file: {str(e)}")
                
                # Process transcripts
                print("Looking for transcript files...")
                transcript_base = os.path.join(
                    TRANSCRIPT_DIR, 
                    drama_name,
                    f"{drama_name}_ep{idx}"
                )
                
                # Possible transcript files
                transcript_files = [
                    f"{transcript_base}_English.txt",
                    f"{transcript_base}_Urdu_T.txt",
                    f"{transcript_base}_Urdu.txt"
                ]
                
                print(f"Checking for transcript files...")
                
                # Process transcripts if they exist
                transcript_count = 0
                for transcript_file in transcript_files:
                    if os.path.exists(transcript_file):
                        print(f"Found transcript: {transcript_file}")
                        
                        # Upload transcript to S3
                        transcript_filename = os.path.basename(transcript_file)
                        s3_transcript_path = f"/transcripts/{drama_name}/{transcript_filename}"
                        tr_url = self.s3.upload_file(transcript_file, s3_transcript_path)
                        
                        if tr_url:
                            print(f"✓ Uploaded transcript to S3: {tr_url}")
                            transcript_count += 1
                    
                if transcript_count == 0:
                    print("No transcript files found")
                else:
                    print(f"✓ Processed {transcript_count} transcript files")
                
                # Try to clean up the temporary directory
                try:
                    os.rmdir(episode_dir)
                except:
                    pass
                    
                # Mark as processed
                self.processed_episodes.add(episode_key)
                print(f"✓ Marked episode as processed")
                print(f"--------- FINISHED {drama_name} Episode {idx} ---------\n")
                return True
            else:
                logger.error(f"Failed to download episode {idx}")
                print(f"✗ Failed to download episode {idx}")
                # Clean up temporary directory on failure
                try:
                    os.rmdir(episode_dir)
                except:
                    pass
        except Exception as e:
            logger.error(f"Episode processing error: {str(e)}")
            print(f"✗ Error processing episode: {str(e)}")
            # Clean up temporary directory on error
            try:
                os.rmdir(episode_dir)
            except:
                pass
        
        return False
    
    def process_drama_sequentially(self, drama_name):
        """Process a single drama with episodes in sequence"""
        print(f"\n\n========== STARTING DRAMA: {drama_name} ==========")
        logger.info(f"Processing drama: {drama_name}")
        
        print(f"Getting playlist information for {drama_name}...")
        data = dramas[drama_name]
        print(f"Playlist URL: {data['link']}")
        
        try:
            # Get video URLs
            video_urls = []
            total_episodes = 0
            
            # Try using yt-dlp first (most reliable)
            if self.yt_dlp_available:
                print("Getting playlist info with yt-dlp...")
                try:
                    cmd = ["yt-dlp", "--flat-playlist", "--get-id", data['link']]
                    result = subprocess.run(cmd, capture_output=True, text=True)
                    if result.returncode == 0:
                        video_ids = result.stdout.strip().split("\n")
                        video_urls = [f"https://www.youtube.com/watch?v={vid}" for vid in video_ids if vid]
                        total_episodes = len(video_urls)
                        print(f"Found {total_episodes} episodes using yt-dlp")
                    else:
                        print(f"yt-dlp playlist extraction failed: {result.stderr}")
                except Exception as e:
                    print(f"yt-dlp playlist extraction error: {str(e)}")
            
            # Fallback to pytube if yt-dlp failed
            if not video_urls:
                try:
                    print("Falling back to pytube for playlist extraction...")
                    playlist = Playlist(data['link'])
                    playlist._video_regex = re.compile(r"\"url\":\"(/watch\?v=[\w-]*)")
                    video_urls = list(playlist.video_urls)
                    total_episodes = len(video_urls)
                    print(f"Found {total_episodes} episodes using pytube")
                except Exception as e:
                    print(f"Pytube playlist extraction error: {str(e)}")
            
            if not video_urls:
                print("No videos found in playlist. Aborting drama processing.")
                return
            
            # Process episodes one by one in sequence
            successful_episodes = 0
            for idx, url in enumerate(video_urls, 1):
                print(f"\n{'='*50}")
                print(f"PROCESSING EPISODE {idx}/{total_episodes}")
                print(f"{'='*50}")
                if self.process_episode(drama_name, idx, url):
                    successful_episodes += 1
                
                # Short delay between episodes
                delay = REQUEST_DELAY
                print(f"Waiting {delay} seconds before next episode...")
                time.sleep(delay)
            
            print(f"\n========== COMPLETED DRAMA: {drama_name} ==========")
            print(f"Successfully processed {successful_episodes} out of {total_episodes} episodes\n\n")
            logger.info(f"Completed drama {drama_name}: {successful_episodes}/{total_episodes} episodes")
            
        except Exception as e:
            print(f"ERROR processing drama {drama_name}: {str(e)}")
            logger.error(f"Drama processing error: {str(e)}")
    
    def process_all_dramas(self):
        """Process all dramas one by one sequentially"""
        logger.info("Starting video download process for all dramas")
        print("\n" + "="*50)
        print("===== DRAMA DOWNLOAD PROCESS STARTED =====")
        print("="*50)
        
        total_dramas = len(dramas)
        print(f"Found {total_dramas} dramas to process:")
        for i, drama_name in enumerate(dramas, 1):
            print(f"  {i}. {drama_name}")
        
        # Process each drama sequentially
        completed_dramas = 0
        for idx, drama_name in enumerate(dramas, 1):
            print(f"\n\n{'#'*60}")
            print(f"### DRAMA {idx}/{total_dramas}: {drama_name}")
            print(f"{'#'*60}")
            
            try:
                self.process_drama_sequentially(drama_name)
                completed_dramas += 1
            except Exception as e:
                print(f"Error processing drama {drama_name}: {str(e)}")
                logger.error(f"Fatal error in drama {drama_name}: {str(e)}")
        
        print("\n" + "="*50)
        print("===== DRAMA DOWNLOAD PROCESS COMPLETED =====")
        print(f"Successfully processed {completed_dramas}/{total_dramas} dramas")
        print("="*50)
        logger.info(f"Completed processing all dramas: {completed_dramas}/{total_dramas}")


if __name__ == "__main__":
    # Make sure transcript directory exists for searching transcripts
    os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
    
    print("\nInitializing downloader...")
    downloader = VideoDownloader()
    
    print("\nStarting drama processing...")
    downloader.process_all_dramas()
    
    print(f"\nScript completed at: {time.strftime('%Y-%m-%d %H:%M:%S')}")