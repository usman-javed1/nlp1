import os
import re
import time
import json
import threading
import logging
import requests
import subprocess
import tempfile
import concurrent.futures
import boto3
from dotenv import load_dotenv
import random
from http.cookiejar import MozillaCookieJar

# Load environment variables from .env file
load_dotenv()

# Configuration
MAX_RETRY_ATTEMPTS = 5
REQUEST_DELAY = 2
TEMP_DIR = tempfile.gettempdir()  
TRANSCRIPT_DIR = "transcripts"      # Only for finding transcripts, not storing
MAX_THREADS = 4
INSTANCE_ID = os.environ.get("AWS_INSTANCE_ID", f"worker-{threading.get_native_id()}")
STRICT_MODE = False

# Set a minimal file size (in bytes) to consider the download valid (e.g., 1 MB)
MIN_VIDEO_SIZE = 1024 * 1024  # 1 MB

# AWS S3 credentials from .env file
AWS_ACCESS_KEY_ID1 = os.environ.get("AWS_ACCESS_KEY_ID1")
AWS_SECRET_ACCESS_KEY1 = os.environ.get("AWS_SECRET_ACCESS_KEY1")
AWS_REGION1 = os.environ.get("AWS_REGION1", "us-east-1")  # Default to us-east-1 if not specified
S3_BUCKET1 = os.environ.get("S3_BUCKET1")
S3_COORD_BUCKET1 = os.environ.get("S3_COORD_BUCKET1")

print(f"AWS_ACCESS_KEY_ID1: {AWS_ACCESS_KEY_ID1}")
print(f"AWS_SECRET_ACCESS_KEY1: {AWS_SECRET_ACCESS_KEY1}")
print(f"S3_BUCKET1: {S3_BUCKET1}")
print(f"S3_COORD_BUCKET1: {S3_COORD_BUCKET1}")
print(f"STRICT_MODE: {STRICT_MODE}")

try:
    from transcript_fetcher import dramas, url_to_id, get_video_info, extract_episode_number
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
        
        if not AWS_ACCESS_KEY_ID1 or not AWS_SECRET_ACCESS_KEY1 or not S3_BUCKET1:
            raise Exception("AWS credentials or bucket name missing from .env file")
            
        try:
            self.s3_client = boto3.client(
                's3',
                aws_access_key_id=AWS_ACCESS_KEY_ID1,
                aws_secret_access_key=AWS_SECRET_ACCESS_KEY1,
                region_name=AWS_REGION1
            )
            # Test connection by listing buckets
            self.s3_client.list_buckets()
            print(f"✓ Successfully connected to AWS S3")
            print(f"✓ Using bucket: {S3_BUCKET1}")
            # Verify bucket exists
            self.s3_client.head_bucket(Bucket=S3_BUCKET1)
            print(f"✓ Bucket {S3_BUCKET1} verified")
            
        except Exception as e:
            logger.error(f"S3 initialization error: {str(e)}")
            raise Exception(f"Failed to initialize S3: {str(e)}")
    
    def upload_file(self, local_path, remote_path):
        """Upload a file to S3 and return the URL"""
        try:
            s3_key = remote_path.lstrip('/')
            print(f"Uploading file to S3: {local_path} → s3://{S3_BUCKET1}/{s3_key}")
            file_size = os.path.getsize(local_path) / (1024 * 1024)
            print(f"File size: {file_size:.2f} MB")
            
            self.s3_client.upload_file(
                local_path, 
                S3_BUCKET1, 
                s3_key,
                ExtraArgs={'ACL': 'public-read'}
            )
            s3_url = f"https://{S3_BUCKET1}.s3.{AWS_REGION1}.amazonaws.com/{s3_key}"
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
        
        # Check for yt-dlp availability
        self.yt_dlp_available = False
        try:
            result = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True)
            if result.returncode == 0:
                self.yt_dlp_available = True
                print(f"Found yt-dlp version: {result.stdout.strip()}")
            else:
                print("yt-dlp not found. Will use fallback methods.")
        except Exception:
            print("yt-dlp not found. Will use fallback methods.")
        
        # Initialize S3 uploader
        self.s3 = S3Uploader()
        self.processed_episodes = set()
        self._setup_rotating_headers()
        self.proxy_pool = [
            None,  # Direct connection
            'http://185.199.229.156:9292',
            'http://185.199.228.220:9292'
        ]
        self.current_proxy = 0
        
        # Get the directory of the current script
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.cookie_path = os.path.join(self.base_dir, 'cookies.txt')
        self._verify_cookie_file()
    
    def _verify_cookie_file(self):
        """Ensure cookies exist and are valid"""
        if not os.path.exists(self.cookie_path):
            raise FileNotFoundError(f"Missing cookies.txt at {self.cookie_path}")
            
        file_size = os.path.getsize(self.cookie_path)
        if file_size < 100:
            print(f"⚠ Warning: cookies.txt seems small ({file_size} bytes). YouTube access may fail.")

    def _get_ytdlp_command(self, url, output_path):
        """Build yt-dlp command with cookies"""
        return [
            'yt-dlp',
            '--cookies', self.cookie_path,
            '--user-agent', self._random_user_agent(),
            '-f', 'best[height<=720]',
            '-o', output_path,
            '--no-playlist',
            url
        ]

    def _create_authenticated_session(self):
        """Create requests session with cookies"""
        session = requests.Session()
        cookies = MozillaCookieJar()
        cookies.load(self.cookie_path, ignore_discard=True)
        session.cookies = cookies
        session.headers.update({
            'User-Agent': self._random_user_agent(),
            'Referer': 'https://www.youtube.com/',
        })
        return session

    def _setup_rotating_headers(self):
        self.user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36'
        ]
        self.headers = {
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.youtube.com/',
            'Origin': 'https://www.youtube.com',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'same-origin',
            'Sec-Fetch-Site': 'same-origin'
        }

    def _rotate_user_agent(self):
        self.headers['User-Agent'] = random.choice(self.user_agents)

    def _get_proxy(self):
        proxy = self.proxy_pool[self.current_proxy % len(self.proxy_pool)]
        self.current_proxy += 1
        return {'http': proxy, 'https': proxy} if proxy else None

    def check_subtitles(self, url):
        if not self.yt_dlp_available or not STRICT_MODE:
            return True
        
        try:
            cmd = ["yt-dlp", "--list-subs", "--skip-download", url]
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode != 0:
                print(f"Failed to check subtitles: {result.stderr}")
                return not STRICT_MODE
            
            output = result.stdout.lower()
            
            if "has no subtitles" in output:
                print("❌ Video has no subtitles")
                return False
                
            if "available automatic captions" in output and "available subtitles" not in output:
                print("❌ Video only has auto-generated subtitles")
                return False
                
            if "available subtitles" in output:
                print("✓ Video has manual subtitles")
                return True
                
            return not STRICT_MODE
            
        except Exception as e:
            print(f"Error checking subtitles: {str(e)}")
            return not STRICT_MODE
    
    def download_video(self, url, output_path):
        """Multi-strategy download with automatic bot bypass"""
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        # Strategy 1: yt-dlp with rotating configurations
        if self.yt_dlp_available:
            for attempt in range(3):
                try:
                    self._rotate_user_agent()
                    cmd = self._get_ytdlp_command(url, output_path)
                    print(f"Attempt {attempt+1} with yt-dlp: {' '.join(cmd)}")
                    
                    result = subprocess.run(
                        cmd,
                        check=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True
                    )
                    if os.path.exists(output_path) and os.path.getsize(output_path) >= MIN_VIDEO_SIZE:
                        print("✓ yt-dlp download successful")
                        return output_path
                except subprocess.CalledProcessError as e:
                    print(f"yt-dlp attempt {attempt+1} failed: {e.output[:200]}...")
                time.sleep(random.uniform(1, 3))

        # Strategy 2: Pytube with header rotation
        for attempt in range(2):
            try:
                from pytube import YouTube
                self._rotate_user_agent()
                print(f"Trying pytube with UA: {self.headers['User-Agent']}")
                
                yt = YouTube(
                    url,
                    use_oauth=True,
                    allow_oauth_cache=True,
                    proxies=self._get_proxy(),
                    headers=self.headers
                )
                yt.bypass_age_gate()
                stream = yt.streams.filter(
                    progressive=True,
                    file_extension='mp4'
                ).order_by('resolution').desc().first()
                
                # Download with session
                with requests.Session() as s:
                    s.headers.update(self.headers)
                    response = s.get(stream.url, proxies=self._get_proxy(), stream=True)
                    with open(output_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            f.write(chunk)
                            
                    if os.path.exists(output_path) and os.path.getsize(output_path) >= MIN_VIDEO_SIZE:
                        print("✓ Pytube download successful")
                        return output_path
            except Exception as e:
                print(f"Pytube attempt {attempt+1} failed: {str(e)}")
                time.sleep(1)

        # Strategy 3: Direct download with session management
        with requests.Session() as s:
            s.headers.update(self.headers)
            for attempt in range(2):
                try:
                    video_id = url.split("v=")[1].split("&")[0]
                    embed_url = f"https://www.youtube.com/embed/{video_id}"
                    
                    # Simulate browser navigation
                    s.get(embed_url, proxies=self._get_proxy())
                    time.sleep(random.uniform(0.5, 2))
                    response = s.get(url, proxies=self._get_proxy())
                    
                    # Find video URL in page
                    match = re.search(r'"url":"(https://[^"]+googlevideo[^"]+)"', response.text)
                    if not match:
                        continue
                        
                    video_url = match.group(1)
                    print(f"Found direct video URL: {video_url[:60]}...")
                    
                    # Download chunk
                    with open(output_path, 'wb') as f:
                        response = s.get(video_url, stream=True, proxies=self._get_proxy())
                        for chunk in response.iter_content(chunk_size=8192):
                            f.write(chunk)
                            
                    if os.path.exists(output_path) and os.path.getsize(output_path) >= MIN_VIDEO_SIZE:
                        print("✓ Direct download successful")
                        return output_path
                        
                except Exception as e:
                    print(f"Direct download attempt {attempt+1} failed: {str(e)}")
                    time.sleep(1)

        print("⚠ All download methods failed")
        return None

    def process_episode(self, drama_name, url, episodes_list, max_episode, order_index=None):
        """
        Process a single episode: verify the extracted episode number from the title is in episodes_list.
        If so, download and upload the video.
        """
        duration, title = get_video_info(url)
        if not title:
            print("❌ Could not retrieve video title, skipping episode")
            return False
        
        ep_num = extract_episode_number(title, max_episode)
        print(f"Episode number: {ep_num}")
        if ep_num is None:
            print("❌ Could not extract episode number, skipping episode")
            return False
        
        if ep_num not in episodes_list:
            print(f"⏭️ Episode {ep_num} is not in the download list {episodes_list}. Skipping.")
            return False
        
        episode_key = f"{drama_name}_ep{ep_num}"
        if episode_key in self.processed_episodes:
            print(f"⚠ Episode {ep_num} already processed. Skipping.")
            return True
        
        print(f"Processing {drama_name} - Episode {ep_num}")
        print(f"Video URL: {url}")
        
        episode_dir = os.path.join(TEMP_DIR, f"drama_{int(time.time())}_{threading.get_native_id()}")
        os.makedirs(episode_dir, exist_ok=True)
        
        try:
            video_id = url_to_id(url)
            output_filename = f"{drama_name}_Ep{ep_num}.mp4"
            output_path = os.path.join(episode_dir, output_filename)
            
            downloaded_path = self.download_video(url, output_path)
            if not downloaded_path:
                logger.error(f"Failed to download episode {ep_num}")
                return False
            
            file_size = os.path.getsize(downloaded_path) / (1024 * 1024)
            print(f"Downloaded video size: {file_size:.2f} MB")
            
            remote_path = f"/videos/{drama_name}/{output_filename}"
            s3_url = self.s3.upload_file(downloaded_path, remote_path)
            if s3_url:
                print(f"✓ Video uploaded to S3: {s3_url}")
            else:
                print(f"✗ Failed to upload video to S3")
                return False
            
            try:
                os.remove(downloaded_path)
                print(f"✓ Removed temporary file: {downloaded_path}")
            except Exception as e:
                print(f"⚠ Error removing temporary file: {str(e)}")
            
            print("Looking for transcript files...")
            transcript_base = os.path.join(
                TRANSCRIPT_DIR, 
                drama_name,
                f"{drama_name}_ep{ep_num}"
            )
            transcript_files = [
                f"{transcript_base}_English.txt",
                f"{transcript_base}_Urdu_T.txt",
                f"{transcript_base}_Urdu.txt"
            ]
            
            transcript_count = 0
            for transcript_file in transcript_files:
                if os.path.exists(transcript_file):
                    print(f"Found transcript: {transcript_file}")
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
            
            try:
                os.rmdir(episode_dir)
            except:
                pass
            
            self.processed_episodes.add(episode_key)
            print(f"✓ Marked episode as processed: {episode_key}")
            print(f"--------- FINISHED {drama_name} Episode {ep_num} ---------\n")
            return True
        
        except Exception as e:
            logger.error(f"Episode processing error: {str(e)}")
            print(f"✗ Error processing episode: {str(e)}")
            try:
                os.rmdir(episode_dir)
            except:
                pass
            return False
    
    def process_drama_sequentially(self, drama_name):
        """Process a single drama by iterating over its playlist and downloading only specified episodes"""
        print(f"\n\n========== STARTING DRAMA: {drama_name} ==========")
        logger.info(f"Processing drama: {drama_name}")
        
        data = dramas[drama_name]
        print(f"Playlist URL: {data['link']}")
        
        try:
            episodes_list, max_episode = data['episodes']
        except Exception as e:
            print(f"Error reading episodes data: {str(e)}")
            return
        
        video_urls = []
        total_episodes = 0
        
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
        
        if not video_urls:
            try:
                print("Falling back to pytube for playlist extraction...")
                from pytube import Playlist
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
        
        successful_episodes = 0
        for url in video_urls:
            print(f"\n{'='*50}")
            print(f"PROCESSING VIDEO: {url}")
            print(f"{'='*50}")
            if self.process_episode(drama_name, url, episodes_list, max_episode):
                successful_episodes += 1
            
            print(f"Waiting {REQUEST_DELAY} seconds before next video...")
            time.sleep(REQUEST_DELAY)
        
        print(f"\n========== COMPLETED DRAMA: {drama_name} ==========")
        print(f"Successfully processed {successful_episodes} out of {total_episodes} videos\n\n")
        logger.info(f"Completed drama {drama_name}: {successful_episodes}/{total_episodes} videos processed")
    
    def process_all_dramas(self):
        """Process all dramas sequentially, downloading only videos matching the episodes array"""
        logger.info("Starting video download process for all dramas")
        print("\n" + "="*50)
        print("===== DRAMA DOWNLOAD PROCESS STARTED =====")
        print("="*50)
        
        total_dramas = len(dramas)
        print(f"Found {total_dramas} dramas to process:")
        for i, drama_name in enumerate(dramas, 1):
            print(f"  {i}. {drama_name}")
        
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

    def _random_user_agent(self):
        """Rotate user agents to avoid detection"""
        agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15',
            'Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0'
        ]
        return random.choice(agents)

if __name__ == "__main__":
    os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
    
    print("\nInitializing downloader...")
    downloader = VideoDownloader()
    
    print("\nStarting drama processing...")
    downloader.process_all_dramas()
    
    print(f"\nScript completed at: {time.strftime('%Y-%m-%d %H:%M:%S')}")
