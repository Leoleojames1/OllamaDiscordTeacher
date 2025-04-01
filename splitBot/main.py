import os
import asyncio
import logging
import re
from datetime import datetime, timezone, UTC
import json
from pathlib import Path
from collections import defaultdict

from dotenv import load_dotenv
from discord import Intents, Message, Game, Status, File
from discord.ext import commands, tasks

# Import our modules
from utils import (
    send_in_chunks, get_user_key, store_user_conversation, 
    process_file_attachment, SYSTEM_PROMPT
)
from services import get_ollama_response
from commands import register_commands

# Load environment variables from .env file
load_dotenv()

# Initialize logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration variables
TOKEN = os.getenv('DISCORD_TOKEN')
DATA_DIR = os.getenv('DATA_DIR', 'data')
CHANGE_NICKNAME = True  # Set to True to change nickname, False to keep the default

# Create data directories
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
Path(f"{DATA_DIR}/searches").mkdir(parents=True, exist_ok=True)
Path(f"{DATA_DIR}/papers").mkdir(parents=True, exist_ok=True)
Path(f"{DATA_DIR}/crawls").mkdir(parents=True, exist_ok=True)
Path(f"{DATA_DIR}/links").mkdir(parents=True, exist_ok=True)

# User profile directory
USER_PROFILES_DIR = os.path.join(DATA_DIR, 'user_profiles')
Path(USER_PROFILES_DIR).mkdir(parents=True, exist_ok=True)

# Global conversation tracking
conversation_logs = [{'role': 'system', 'content': SYSTEM_PROMPT}]
USER_CONVERSATIONS = defaultdict(lambda: [{'role': 'system', 'content': SYSTEM_PROMPT}])
COMMAND_MEMORY = defaultdict(dict)  # Stores persistent memory for commands

def get_prefix(bot, message):
    """Get the command prefix for the bot."""
    # Only respond to commands if the bot is mentioned
    if bot.user and bot.user.mentioned_in(message):
        content = re.sub(f'<@!?{bot.user.id}>', '', message.content).strip()
        if content.startswith('!'):
            return '!'
    return commands.when_mentioned(bot, message)

# Initialize the bot
intents = Intents.default()
intents.message_content = True  # Explicitly enable message content intent
bot = commands.Bot(command_prefix=get_prefix, intents=intents)
bot.remove_command('help')  # Remove the default help command

# Register all command handlers
register_commands(bot, USER_CONVERSATIONS, COMMAND_MEMORY, conversation_logs, USER_PROFILES_DIR)

@bot.event
async def on_message(message: Message):
    """Handles incoming messages."""
    # Don't respond to self
    if message.author == bot.user:
        return

    # Get user's preferred name
    user_name = message.author.display_name or message.author.name
    
    # Only process if bot is mentioned
    if bot.user and bot.user.mentioned_in(message):
        content = re.sub(f'<@!?{bot.user.id}>', '', message.content).strip()
        
        # Store user information
        await store_user_conversation(message, content)
        
        # Add personalized greeting for direct questions
        if not content.startswith('!'):
            greeting = f"Hi {user_name}! "
        else:
            greeting = ""
        
        # Process commands if starts with !
        if content.startswith('!'):
            message.content = content
            await bot.process_commands(message)
        # Handle conversation for non-command mentions
        else:
            try:
                # Handle file attachments
                if message.attachments:
                    files_content = []
                    for attachment in message.attachments:
                        try:
                            file_content = await process_file_attachment(attachment)
                            files_content.append(f"File: {attachment.filename}\n{file_content}")
                        except ValueError as e:
                            await message.channel.send(f"⚠️ {user_name}, there was an error with {attachment.filename}: {str(e)}")
                            continue
                    
                    if files_content:
                        # Combine file contents with the question
                        combined_prompt = f"""The user {user_name} has provided these file(s) to analyze:

{chr(10).join(files_content)}

Their question or request is: {content}

Please provide a detailed response, including code examples if relevant. Address the user by name in your response."""
                        
                        conversation_logs.append({'role': 'user', 'content': combined_prompt})
                        async with message.channel.typing():
                            response = await get_ollama_response(combined_prompt)
                        conversation_logs.append({'role': 'assistant', 'content': response})
                        await store_user_conversation(message, response, is_bot=True)
                        await send_in_chunks(message.channel, greeting + response, message)
                        return
                
                # Regular conversation without files
                personalized_content = f"{user_name} asks: {content}\n\nProvide a helpful response, addressing them by name."
                conversation_logs.append({'role': 'user', 'content': personalized_content})
                async with message.channel.typing():
                    response = await get_ollama_response(personalized_content)
                conversation_logs.append({'role': 'assistant', 'content': response})
                await store_user_conversation(message, response, is_bot=True)
                await send_in_chunks(message.channel, greeting + response, message)
            
            except Exception as e:
                logging.error(f"Error processing message: {e}")
                await message.channel.send(f"⚠️ {user_name}, an error occurred: {str(e)}")

async def change_nickname(guild):
    """Change the bot's nickname in the specified guild."""
    nickname = f"Ollama Teacher"
    try:
        await guild.me.edit(nick=nickname)
        logging.info(f"Nickname changed to {nickname} in guild {guild.name}")
    except Exception as e:
        logging.error(f"Failed to change nickname in guild {guild.name}: {str(e)}")

@bot.event
async def on_ready():
    """Called when the bot is ready."""
    try:
        # Log startup
        logging.info(f'{bot.user.name} is now running!')
        logging.info(f'Connected to {len(bot.guilds)} guilds')
        
        # Start periodic tasks
        analyze_user_profiles.start()
        
        # Initialize user data storage
        for guild in bot.guilds:
            logging.info(f'Initializing data for guild: {guild.name}')
            guild_dir = Path(f"{DATA_DIR}/guilds/{guild.id}")
            guild_dir.mkdir(parents=True, exist_ok=True)
            
            # Store member information
            member_data = {}
            for member in guild.members:
                if not member.bot:
                    member_data[str(member.id)] = {
                        'name': member.name,
                        'display_name': member.display_name,
                        'joined_at': member.joined_at.isoformat() if member.joined_at else None,
                        'roles': [role.name for role in member.roles if role.name != "@everyone"],
                        'last_active': datetime.now(UTC).isoformat()
                    }
            
            # Save member data
            member_file = guild_dir / 'members.json'
            with open(member_file, 'w', encoding='utf-8') as f:
                json.dump(member_data, f, indent=2)
        
        # Change nicknames if enabled
        if (CHANGE_NICKNAME):
            for guild in bot.guilds:
                try:
                    await guild.me.edit(nick="Ollama Teacher")
                    logging.info(f'Nickname changed in guild {guild.name}')
                except Exception as e:
                    logging.error(f'Failed to change nickname in {guild.name}: {e}')
        
        # Set custom status with help command info
        status_text = "!help | Mention me with questions!"
        await bot.change_presence(
            activity=Game(name=status_text),
            status=Status.online
        )
        
        logging.info('Bot initialization complete!')
        
    except Exception as e:
        logging.error(f'Error in on_ready: {e}')

@tasks.loop(minutes=30)
async def analyze_user_profiles():
    """Analyze user conversations and update profiles periodically."""
    try:
        for user_key, conversations in USER_CONVERSATIONS.items():
            # Skip if doesn't match expected format
            if '_' not in user_key:
                continue
                
            try:
                guild_id, user_id = user_key.split('_')
                user_id = int(user_id)
            except ValueError:
                continue
            
            # Get user messages only
            user_messages = [
                conv['content'] for conv in conversations 
                if conv['role'] == 'user'
            ]
            
            if not user_messages:
                continue
                
            # Create analysis prompt
            analysis_prompt = f"""Analyze these user messages and extract key information:
{chr(10).join(user_messages[-50:])}

Please identify:
1. Main topics of interest
2. Technical skill level
3. Common questions or patterns
4. Learning progress
5. Key concepts discussed

Format the response as concise bullet points."""
            
            # Get AI analysis
            analysis = await get_ollama_response(analysis_prompt, with_context=False)
            
            # Save to user profile
            profile_path = os.path.join(USER_PROFILES_DIR, f"{user_key}_profile.json")
            username = bot.get_user(user_id).name if bot.get_user(user_id) else 'Unknown'
            profile_data = {
                'timestamp': datetime.now(UTC).isoformat(),
                'analysis': analysis,
                'username': username
            }
            
            with open(profile_path, 'w', encoding='utf-8') as f:
                json.dump(profile_data, f, indent=2)
                
    except Exception as e:
        logging.error(f"Error in analyze_user_profiles: {e}")

def main():
    """Main function to run the bot."""
    bot.run(TOKEN)

if __name__ == '__main__':
    main()