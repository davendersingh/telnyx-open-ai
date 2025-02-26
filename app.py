from functools import wraps
import os
import requests
import tempfile
# from flask_cors import CORS
import openai
from flask import Flask, request, jsonify, current_app
import logging

from dotenv import load_dotenv
import telnyx

load_dotenv()

# Initialize Telnyx client for API calls
telnyx.api_key = os.getenv('TELNYX_API_KEY')

# Initialize OpenAI client
openai.api_key = os.getenv('OPENAI_API_KEY')
client = openai.Client()

# Stream url
stream_url = os.getenv('TELNYX_STREAM_URL')

# Set up Flask logger
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
# CORS(app)  # Enable CORS for all routes

# Store conversation context
conversations = {}

@app.route("/test", methods=["GET", "POST"])
def test():
    print("Test endpoint accessed")
    print("Headers:", dict(request.headers))
    return jsonify({
        "status": "ok",
        "headers": dict(request.headers),
        "method": request.method
    }), 200

def validate_webhook(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        webhook_signature = request.headers.get('Telnyx-Signature-Ed25519')
        webhook_timestamp = request.headers.get('Telnyx-Timestamp')
        try:
            telnyx.webhooks.Webhook.verify_signature(
                request.data,
                webhook_signature,
                webhook_timestamp,
                os.getenv('TELNYX_PUBLIC_KEY')
            )
        except Exception as e:
            return jsonify({"status": "unauthorized", "error": str(e)}), 401
        return f(*args, **kwargs)
    return decorated_function

def process_audio_chunk(audio_data, call_control_id):
    try:
        # Transcribe audio using OpenAI Whisper
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_data
        )
        
        if not transcript.text:
            return None
            
        print(f"Transcribed: {transcript.text}")
            
        # Get conversation history or create new
        conversation = conversations.get(call_control_id, [])
        conversation.append({"role": "user", "content": transcript.text})
        
        # Get AI response
        response = client.chat.completions.create(
            model="gpt-4-turbo-preview",  # Latest GPT-4 Turbo model
            messages=[
                {"role": "system", "content": "You are a helpful AI assistant handling a phone call. Keep responses concise, natural, and brief. Aim for responses under 15 words when possible."},
                *conversation
            ],
            max_tokens=50,        # Shorter responses for faster processing
            temperature=0.7,      # Good balance between creativity and focus
            presence_penalty=0.6,  # Encourage topic variation
            frequency_penalty=0.5  # Reduce repetition
        )
        
        ai_response = response.choices[0].message.content
        print(f"AI Response: {ai_response}")
        
        conversation.append({"role": "assistant", "content": ai_response})
        conversations[call_control_id] = conversation
        
        return ai_response
        
    except Exception as e:
        print(f"Error processing audio: {str(e)}")
        return None

@app.route("/webhook", methods=["POST"])
@validate_webhook
def handle_call():
    data = request.get_json()
    logger.info(f"Webhook received: {data.get('data', {}).get('event_type')}")
    
    match data.get("data", {}).get("event_type"):
        case "call.initiated" | "call.received":
            logger.info("Incoming call...")
            call_control_id = data["data"]["payload"]["call_control_id"]
            caller_number = data["data"]["payload"].get("from")
            logger.info(f"Call from {caller_number} with control ID: {call_control_id}")
            
            try:
                call = telnyx.Call()
                call.call_control_id = call_control_id
                call.stream_url = stream_url
        
                # Answer the call
                answer_response = call.answer()
                logger.info(f"Call answer response: {answer_response}")
                
                # Enable streaming
                conversations[call_control_id] = []
                logger.info(f"Streaming enabled for call {call_control_id}")
                
                return jsonify({
                    "data": {
                        "client_state": "streaming",
                        "command_id": call_control_id,
                        "answer": {
                            "streaming": {
                                "enable": True,
                                "track": "inbound",
                                "intervals": 2,  # Interval in seconds between chunks
                                "format": "raw",  # Add format specification
                                "channels": 1     # Mono audio
                            }
                        }
                    }
                }), 200
                
            except Exception as e:
                logger.error(f"Error handling call: {str(e)}", exc_info=True)
                return jsonify({"error": str(e)}), 500
        
        case "call.answered":
            logger.info("Call answered event received")
            logger.info(f"data: {data}")
            call_control_id = data["data"]["payload"]["call_control_id"]
        
            try:
                logger.info("Generating initial greeting...")
        
                # Create a temporary file to store the audio
                with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as temp_audio:
                    # Generate speech and write to temp file
                    speech_response = client.audio.speech.create(
                        model="tts-1",
                        voice="alloy",
                        input="Hello! How can I help you today?",
                        response_format="mp3"
                    )
                    temp_audio.write(speech_response.content)
                    temp_audio.flush()
                    
                    logger.info(f"Generated speech saved to: {temp_audio.name}")
                    
                    # Create FormData-like structure
                    files = {
                        'media': ('greeting.mp3', open(temp_audio.name, 'rb'), 'audio/mpeg')
                    }
                    
                    # Make direct API request to Telnyx
                    upload_url = "https://api.telnyx.com/v2/media"
                    headers = {
                        'Authorization': f'Bearer {os.getenv("TELNYX_API_KEY")}'
                    }
                    
                    response = requests.post(upload_url, headers=headers, files=files)
                    response.raise_for_status()
                    media_data = response.json()
                    logger.info(f"Media uploaded successfully Data: {media_data}")
                    # Construct media URL using media_name
                    media_name = media_data['data']['media_name']
                    media_url = f"https://api.telnyx.com/v2/media/{media_name}/play"

                    logger.info(f"Constructed media URL: {media_url}")
                    
                    # Clean up temp file
                    os.unlink(temp_audio.name)
                    
                    # In media.streaming case:
                    # Play response
                    playback_response = telnyx.Call.create_playback_start(
                        call_control_id,
                        media_name=media_name,
                        loop=1,
                        overlay=False
                    )
                    
                    logger.info(f"Playing response for call {call_control_id}")
            
                return jsonify({"status": "greeting sent"}), 200
        
            except Exception as e:
                logger.error(f"Error in greeting playback: {str(e)}", exc_info=True)
                return jsonify({"error": str(e)}), 500
        
        case "streaming.started":
            print("Streaming started...")
            return jsonify({"status": "streaming active"}), 200
            
        case "media.streaming":
            # Handle incoming audio chunks
            call_control_id = data["data"]["payload"]["call_control_id"]
            logger.info(f"Received media chunk for call {call_control_id}")
            media_chunk = data["data"]["payload"].get("chunk")  # Fix payload path
            logger.info(f"Received media chunk for call {call_control_id}")

            if media_chunk:
                ai_response = process_audio_chunk(media_chunk, call_control_id)
                if ai_response:
                    try:
                        # Generate speech from AI response
                        speech_response = client.audio.speech.create(
                            model="tts-1",
                            voice="alloy",
                            input=ai_response,
                            response_format="mp3"
                        )
                        # Play response using Call.create_playback_start
                        call = telnyx.Call.retrieve(call_control_id)
                        playback_response = call.actions_playback_start(
                            audio_url=speech_response.url,
                            loop="false",  # String value as required
                            overlay=False,
                            target_legs="self"
                        )
                        logger.info(f"Playing AI response for call {call_control_id}")

                    except Exception as e:
                        logger.error(f"Error in audio playback: {str(e)}", exc_info=True)

            return jsonify({"status": "processing"}), 200
            
        case "streaming.stopped":
            print("Streaming stopped...")
            call_control_id = data["data"]["payload"]["call_control_id"]
            if call_control_id in conversations:
                del conversations[call_control_id]
            return jsonify({"status": "streaming ended"}), 200
            
        case "call.hangup":
            print("Call ended...")
            call_control_id = data["data"]["payload"]["call_control_id"]
            if call_control_id in conversations:
                del conversations[call_control_id]
            return jsonify({"status": "call ended"}), 200
        
        case "call.playback.ended":  # Add this case
            logger.info("Playback ended event received")
            call_control_id = data["data"]["payload"]["call_control_id"]
            logger.info(f"Playback ended for call {call_control_id}")
            
            # You might want to add logic here to prompt the user for input
            # or perform other actions after the playback ends.
            
            return jsonify({"status": "playback ended"}), 200
            
    return jsonify({"status": "unhandled event"}), 200


if __name__ == "__main__":
    logger.info("Starting Flask application...")
    app.run(host='0.0.0.0', port=os.getenv('PORT' | 5000), debug=True)