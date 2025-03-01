import os
import re
import requests
from time import sleep
from pytube import Playlist, YouTube
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound

# Define missing constants
RETRY_ATTEMPTS = 3          # Number of retry attempts for HTTP requests and transcript fetching
REQUEST_DELAY = 2           # Delay (in seconds) between retry attempts
MIN_DURATION = 60           # Minimum duration (in seconds) for a video to be processed

def generate_episode_data(total_episodes, max_episodes=None, manual_data=None):
    """
    Returns a tuple (episodes_list, max_episode)
    If manual_data is provided, use that as the list of episodes and set max_episode accordingly.
    Otherwise, generate a list from 1 to total_episodes.
    """
    if manual_data:
        if max_episodes is None:
            max_episodes = max(manual_data)
        return (manual_data, max_episodes)
    return ([i + 1 for i in range(total_episodes)], max_episodes)

dramas = {
    "Daraar": {
        "link": "https://www.youtube.com/playlist?list=PLdZNFVCDo_1cOWnp-bw3x8CxOw7bMxRt-",
        "episodes": generate_episode_data(None, 40, [1, 2, 4, 6, 9, 40, 39])
    },
    "Baichain Dil": {
        "link": "https://www.youtube.com/playlist?list=PLB1BPYz25JSpGfcskNyX0DmwlXcNOvyT4",
        "episodes": generate_episode_data(37, 37)
    },
    "Main Na Janoo": {
        "link": "https://www.youtube.com/watch?v=5Cun41G44dc&list=PLbVdwtmx18sviyRcmCCQirArY5DR1doQQ&index=34",
        "episodes": generate_episode_data(31, 31)
    },
    "Parizaad": {
        "link": "https://www.youtube.com/watch?v=fwZ6JNfXezg&list=PLbVdwtmx18stXNeBl2fTxbHUsP-HbIYth",
        "episodes": generate_episode_data(29, 29)
    },
    "Qabeel": {
        "link": "https://www.youtube.com/watch?v=4xUvwCzhyQs&list=PLqunGGXHQ5sEsPa8fkFyzvzxUd0e8FRv_&index=1",
        "episodes": generate_episode_data(1, 1)
    },
    "Aye Ishq E Junoon": {
        "link": "https://www.youtube.com/watch?v=_p8bCk8pEv4&list=PLb2aaNHUy_gGbfcGbIOIDbWmpXVpurgGh&index=1",
        "episodes": generate_episode_data(32, 32)
    },
    "Sotan": {
        "link": "https://www.youtube.com/watch?v=1HlBsY_7KOE&list=PLz2MrXbUSiBoaGl0Ia2Q-_G8md6k8DegO",
        "episodes": generate_episode_data(58, 58)
    },
    "Zard Patton Ka Bunn": {
        "link": "https://www.youtube.com/watch?v=Y3bPhqTEGSY&list=PLbVdwtmx18su3GY_B7miQbxmhbVh9KTDn",
        "episodes": generate_episode_data(29, 29)
    },
    "Darlings": {
        "link": "https://www.youtube.com/watch?v=Gr9UyxQYjO4&list=PLQTepLZOvCg5jD7ljW8Eg2C_HJNvGmicV",
        "episodes": generate_episode_data(55, 55)
    },
    "Kaisa Mera Naseeb": {
        "link": "https://www.youtube.com/watch?v=XI8TJxKc3Kw&list=PLz2MrXbUSiBoojRUSDm1dUi4RdUIDtwXa",
        "episodes": generate_episode_data(8, 8)
    },
    "Akhara": {
        "link": "https://www.youtube.com/watch?v=3ZZn3haoRFA&list=PLs2CG9JU32b7iF3Iszyd63vxm47qeYysE",
        "episodes": generate_episode_data(34, 34)
    },
    "Mohabbatain Chahatain": {
        "link": "https://www.youtube.com/watch?v=soj9FDuHBGU&list=PLeb83ChrfOzkYh3FJFiZ5hW8uZj6yaJ79&index=47",
        "episodes": generate_episode_data(6, 6)
    },
    "Jaan Se Pyara Juni": {
        "link": "https://www.youtube.com/watch?v=FQxDh-pKXj0&list=PLbVdwtmx18sv59ZlGX7qmAj65AXF5iRNu",
        "episodes": generate_episode_data(34, 34)
    },
    "Me Kahani Hun": {
        "link": "https://www.youtube.com/watch?v=hLRuSVJ_Ynk&list=PLeb83ChrfOzkFzkenCQthTFLgPB5FsLan&index=12",
        "episodes": generate_episode_data(12, 12)
    },
    "Tere Bina Mein Nahi": {
        "link": "https://www.youtube.com/watch?v=8o7xs7MLpQA&list=PLb2aaNHUy_gHLxFkFX4uFSx-P4vxZ7jBr",
        "episodes": generate_episode_data(39, 39)
    },
    "Umm-e-Haniya": {
        "link": "https://www.youtube.com/watch?v=YxIb_BNJkI0&list=PLdZNFVCDo_1cFNYaFX9C5ZuQ3ZkL3nFGT&index=2",
        "episodes": generate_episode_data(38, 38)
    },
    "Besharam": {
        "link": "https://www.youtube.com/watch?v=kLamSiob72Y&list=PL3y6etwW5z8JxbJp64nA4fmsF_7mgeJai",
        "episodes": generate_episode_data(24, 24)
    },
}

def get_video_info(url):
    """EC2-optimized video info retrieval with consistent title formatting"""
    try:
        # First try through YouTube API
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9'
        }
        
        for _ in range(3):  # Retry for EC2 flakiness
            response = requests.get(f'https://www.youtube.com/oembed?url={url}&format=json', 
                                   headers=headers)
            if response.status_code == 200:
                data = response.json()
                title = data['title']
                print(f"[EC2 Debug] API Title: {title}")
                # Standardize title format across environments
                title = re.sub(r'\s*-\s*YouTube$', '', title)
                return _get_duration(url), title
                
        # Fallback to HTML parsing
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            title_match = re.search(r'<meta name="title" content="([^"]+)"', response.text)
            if title_match:
                title = title_match.group(1)
                print(f"[EC2 Debug] HTML Title: {title}")
                return _get_duration(url), title
                
    except Exception as e:
        print(f"Video info error: {str(e)}")

    # Final fallback to pytube
    try:
        yt = YouTube(url)
        print(f"[EC2 Debug] Pytube Title: {yt.title}")
        return yt.length, yt.title
    except Exception as e:
        print(f"Pytube fallback failed: {str(e)}")
        return 0, 'Unknown Title'

def extract_episode_number(title, max_episode=None):
    """Enhanced extraction for EC2 numeric titles"""
    print(f"\nRaw title received: {title}")
    
    # Handle numeric titles (e.g., "45K")
    if re.match(r'^\d+[kK]?$', title):
        num = int(re.sub('[^0-9]', '', title))
        print(f"Matched numeric title: {num}")
        return num if max_episode and num <= max_episode else num
    
    # Original extraction logic for formatted titles
    patterns = [
        (r'Episode (\d+)', "Standard episode"),
        (r'\bEp(?:isode)?[ ]?(\d+)', "Ep prefix"),
        (r'\bE(\d+)\b', "E prefix"),
        (r'(\d+)(?:st|nd|rd|th) Episode', "Ordinal episode")
    ]
    
    for pattern, desc in patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            num = int(match.group(1))
            print(f"Matched {desc}: {num}")
            return num if max_episode and num <= max_episode else num
            
    print("No patterns matched")
    return None

def get_transcripts(video_id):
    """Get transcripts with auto-translate fallback"""
    try:
        transcripts = YouTubeTranscriptApi.list_transcripts(video_id)
    except (TranscriptsDisabled, NoTranscriptFound):
        return None, None

    en_transcript = ur_transcript = None
    
    # English transcript
    try:
        en_transcript = transcripts.find_transcript(['en'])
    except NoTranscriptFound:
        pass
        
    # Urdu transcript (original or translated)
    try:
        ur_transcript = transcripts.find_transcript(['ur'])
    except NoTranscriptFound:
        if en_transcript:
            try:  # Auto-translate from English
                ur_transcript = en_transcript.translate('ur')
            except Exception:
                pass

    return (
        en_transcript.fetch() if en_transcript else None,
        ur_transcript.fetch() if ur_transcript else None
    )

def url_to_id(url):
    """Extract video ID from URL"""
    patterns = [
        r"v=([\w-]{11})",
        r"be/([\w-]{11})",
        r"embed/([\w-]{11})",
        r"/([\w-]{11})$"
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return url.split('/')[-1]

def save_transcript(transcript, filename, with_timestamps=True):
    """Save transcript to file"""
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    with open(filename, 'w', encoding='utf-8') as f:
        if with_timestamps:
            for entry in transcript:
                f.write(f"[{entry['start']:.2f}] {entry['text']}\n")
        else:
            f.write(' '.join([entry['text'] for entry in transcript]))

def process_dramas():
    print("🚀 Starting transcript processing...")
    
    for drama_name, data in dramas.items():
        print(f"\n📺 Processing drama: {drama_name}")
        playlist = Playlist(data['link'])
        # Adjust regex for video URL extraction (if needed)
        playlist._video_regex = re.compile(r'"url":"(/watch\?v=[\w-]*)')
        
        print(f"🔍 Found {len(playlist.video_urls)} videos")
        episodes_list, max_episode = data['episodes']
        
        for url in playlist.video_urls:
            print(f"\n📼 Processing URL: {url}")
            
            # Get video info
            duration, title = get_video_info(url)
            if not title:
                print("❌ Could not retrieve video title, skipping")
                continue
            print(f"📝 Title: {title}")
            
            # Extract episode number (including checks for last or 2nd last)
            ep_num = extract_episode_number(title, max_episode)
            if ep_num is None:
                print("❌ Could not extract episode number, skipping")
                continue
            print(f"🔢 Extracted episode number: {ep_num}")
            
            # Check if episode is in the list
            if ep_num not in episodes_list:
                print(f"⏭️ Episode {ep_num} not in the download list, skipping")
                continue
            
            # Duration check
            print(f"⏱️  Duration: {duration//60}m {duration%60}s")
            if duration < MIN_DURATION:
                print("⏭️  Skipping short video")
                continue
                
            # Get transcripts
            video_id = url_to_id(url)
            print(f"🔧 Video ID: {video_id}")
            
            en_transcript, ur_transcript = None, None
            for attempt in range(RETRY_ATTEMPTS):
                try:
                    en_transcript, ur_transcript = get_transcripts(video_id)
                    break
                except Exception as e:
                    print(f"Attempt {attempt+1} failed: {str(e)}")
                    sleep(REQUEST_DELAY)

            # Save files if transcripts are available
            base_path = f"transcripts/{drama_name}_Ep_{ep_num}"
            if en_transcript:
                save_transcript(en_transcript, f"{base_path}_English_T.txt")
                save_transcript(en_transcript, f"{base_path}_English.txt", with_timestamps=False)
            if ur_transcript:
                save_transcript(ur_transcript, f"{base_path}_Urdu_T.txt")
                save_transcript(ur_transcript, f"{base_path}_Urdu.txt", with_timestamps=False)
                
            print("✅ Success!" if en_transcript or ur_transcript else "⏭️  No transcripts")

if __name__ == "__main__":
    process_dramas()
