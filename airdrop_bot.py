#!/usr/bin/env python3
"""
Crypto Airdrop Analysis Telegram Bot
======================================
A production-ready bot for discovering, analyzing, and tracking crypto airdrops.

Setup Instructions:
1. Install dependencies: pip install python-telegram-bot python-dotenv aiohttp
2. Create a .env file with: TELEGRAM_BOT_TOKEN=your_token_here
3. Run: python airdrop_bot.py

Optional API Keys (for enhanced data):
- Add COINGECKO_API_KEY or AIRDROP_API_KEY to .env for live data
"""

import os
import sys
import sqlite3
import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, 
    ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from telegram.constants import ParseMode

# Load environment variables
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")
AIRDROP_API_KEY = os.getenv("AIRDROP_API_KEY", "")

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Constants
DB_PATH = "airdrop_bot.db"
ANALYZE_TOKEN = range(1)  # Conversation state for /analyze command

# ============================================================================
# DATABASE SETUP
# ============================================================================

class AirdropDatabase:
    """Manages SQLite database for user preferences and airdrop tracking."""
    
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        """Initialize database schema if it doesn't exist."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Users table: track preferences and notification settings
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                daily_reminders BOOLEAN DEFAULT 0,
                min_risk_score REAL DEFAULT 0.3,
                preferred_chains TEXT DEFAULT 'ethereum,polygon,binance'
            )
        """)
        
        # Airdrops table: cache airdrop data to prevent duplicate alerts
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS airdrops (
                airdrop_id TEXT PRIMARY KEY,
                name TEXT,
                token_symbol TEXT,
                description TEXT,
                status TEXT,
                end_date TIMESTAMP,
                estimated_value REAL,
                risk_score REAL,
                data_json TEXT,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                notified_users TEXT DEFAULT ''
            )
        """)
        
        # Tracking table: which airdrops users have been alerted about
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_airdrops (
                user_id INTEGER,
                airdrop_id TEXT,
                notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, airdrop_id)
            )
        """)
        
        conn.commit()
        conn.close()
        logger.info(f"Database initialized at {self.db_path}")
    
    def add_user(self, user_id: int, username: str = None) -> bool:
        """Add or update a user. Returns True if new user."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        exists = cursor.fetchone() is not None
        
        if not exists:
            cursor.execute(
                "INSERT INTO users (user_id, username) VALUES (?, ?)",
                (user_id, username)
            )
            conn.commit()
            logger.info(f"New user added: {user_id}")
        
        conn.close()
        return not exists
    
    def set_daily_reminders(self, user_id: int, enabled: bool):
        """Enable/disable daily reminders for a user."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET daily_reminders = ? WHERE user_id = ?",
            (enabled, user_id)
        )
        conn.commit()
        conn.close()
    
    def get_reminder_users(self) -> List[int]:
        """Get all users who have enabled daily reminders."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE daily_reminders = 1")
        users = [row[0] for row in cursor.fetchall()]
        conn.close()
        return users
    
    def cache_airdrop(self, airdrop_id: str, name: str, token_symbol: str, 
                     description: str, status: str, end_date: datetime, 
                     estimated_value: float, risk_score: float, data: Dict):
        """Cache airdrop data to prevent duplicate alerts."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO airdrops 
            (airdrop_id, name, token_symbol, description, status, end_date, 
             estimated_value, risk_score, data_json, cached_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (airdrop_id, name, token_symbol, description, status, 
              end_date.isoformat() if end_date else None, estimated_value, 
              risk_score, json.dumps(data)))
        conn.commit()
        conn.close()
    
    def is_user_notified(self, user_id: int, airdrop_id: str) -> bool:
        """Check if a user has already been notified about an airdrop."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM user_airdrops WHERE user_id = ? AND airdrop_id = ?",
            (user_id, airdrop_id)
        )
        result = cursor.fetchone() is not None
        conn.close()
        return result
    
    def mark_user_notified(self, user_id: int, airdrop_id: str):
        """Mark an airdrop as notified for a user."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO user_airdrops (user_id, airdrop_id) VALUES (?, ?)",
            (user_id, airdrop_id)
        )
        conn.commit()
        conn.close()


# ============================================================================
# AIRDROP DATA & API INTEGRATION
# ============================================================================

class AirdropAPI:
    """Manages airdrop data (using mock data - no external APIs needed)."""
    
    # Mock airdrop data for testing without API keys
    MOCK_AIRDROPS = [
        {
            "id": "optimism_retro",
            "name": "Optimism Retroactive Airdrop",
            "symbol": "OP",
            "description": "Early Optimism users eligible for governance token.",
            "status": "active",
            "end_date": (datetime.now() + timedelta(days=30)).isoformat(),
            "tasks": ["Use Optimism dApp"],
            "estimated_value": 500.0,
            "backers": "Sequoia Capital, Polychain",
            "community_size": 250000,
            "task_complexity": "low",
            "chain": "ethereum",
            "created_at": (datetime.now() - timedelta(days=15)).isoformat(),
        },
        {
            "id": "arbitrum_nova",
            "name": "Arbitrum Nova Testnet Badge",
            "symbol": "ARB",
            "description": "Complete Arbitrum Nova testnet transactions for future rewards.",
            "status": "active",
            "end_date": (datetime.now() + timedelta(days=45)).isoformat(),
            "tasks": ["Bridge tokens", "Swap tokens", "Stake tokens"],
            "estimated_value": 800.0,
            "backers": "Sequoia Capital, Paradigm",
            "community_size": 500000,
            "task_complexity": "medium",
            "chain": "ethereum",
            "created_at": (datetime.now() - timedelta(days=8)).isoformat(),
        },
        {
            "id": "zora_genesis",
            "name": "Zora Protocol Genesis NFT",
            "symbol": "ZRA",
            "description": "Mint Zora genesis NFT for protocol governance token.",
            "status": "active",
            "end_date": (datetime.now() + timedelta(days=60)).isoformat(),
            "tasks": ["Mint NFT on Zora"],
            "estimated_value": 1200.0,
            "backers": "a16z Crypto, Multicoin",
            "community_size": 100000,
            "task_complexity": "low",
            "chain": "ethereum",
            "created_at": (datetime.now() - timedelta(days=5)).isoformat(),
        },
        {
            "id": "linea_early",
            "name": "Linea Early Adoption",
            "symbol": "LINEA",
            "description": "Use Linea mainnet during early phase for token allocation.",
            "status": "upcoming",
            "end_date": (datetime.now() + timedelta(days=20)).isoformat(),
            "tasks": ["Transact on Linea"],
            "estimated_value": 300.0,
            "backers": "ConsenSys",
            "community_size": 80000,
            "task_complexity": "low",
            "chain": "ethereum",
            "created_at": (datetime.now() - timedelta(days=2)).isoformat(),
        },
        {
            "id": "sei_testnet",
            "name": "Sei Testnet Participation",
            "symbol": "SEI",
            "description": "Run Sei testnet validator or trader for mainnet rewards.",
            "status": "active",
            "end_date": (datetime.now() + timedelta(days=90)).isoformat(),
            "tasks": ["Run validator", "Execute trades"],
            "estimated_value": 2000.0,
            "backers": "Pantera Capital, Polychain",
            "community_size": 150000,
            "task_complexity": "high",
            "chain": "cosmos",
            "created_at": (datetime.now() - timedelta(days=20)).isoformat(),
        },
    ]
    
    async def fetch_airdrops(self) -> List[Dict]:
        """Return mock airdrop data (no external API calls needed)."""
        logger.info("Using mock airdrop data")
        return self.MOCK_AIRDROPS


# ============================================================================
# AIRDROP ANALYSIS & SCORING
# ============================================================================

class AirdropAnalyzer:
    """Analyzes airdrop risk/reward using a structured scoring framework."""
    
    @staticmethod
    def calculate_risk_score(airdrop: Dict) -> float:
        """
        Calculate risk score (0.0 = lowest risk, 1.0 = highest risk).
        Factors: backer reputation, community size, task complexity, track record.
        """
        score = 0.5  # Start at neutral
        
        # Backer reputation (reduces risk)
        reputable_backers = ["Sequoia", "Paradigm", "a16z", "Polychain", "Pantera", "ConsenSys"]
        if any(backer in airdrop.get("backers", "") for backer in reputable_backers):
            score -= 0.2
        else:
            score += 0.1
        
        # Community size (larger = lower risk)
        community = airdrop.get("community_size", 0)
        if community > 400000:
            score -= 0.15
        elif community < 50000:
            score += 0.15
        
        # Task complexity (high complexity = higher risk of not completing)
        task_complexity = airdrop.get("task_complexity", "medium").lower()
        if task_complexity == "high":
            score += 0.1
        elif task_complexity == "low":
            score -= 0.05
        
        # Status (upcoming = slightly higher risk due to uncertainty)
        if airdrop.get("status") == "upcoming":
            score += 0.1
        
        # Clamp score between 0 and 1
        return max(0.0, min(1.0, score))
    
    @staticmethod
    def get_risk_label(score: float) -> str:
        """Convert risk score to human-readable label."""
        if score < 0.3:
            return "🟢 Low Risk"
        elif score < 0.6:
            return "🟡 Medium Risk"
        else:
            return "🔴 High Risk"
    
    @staticmethod
    def format_airdrop_analysis(airdrop: Dict, risk_score: float) -> str:
        """Format airdrop data into a detailed analysis message."""
        name = airdrop.get("name", "Unknown")
        symbol = airdrop.get("symbol", "N/A")
        description = airdrop.get("description", "No description")
        status = airdrop.get("status", "unknown").upper()
        estimated_value = airdrop.get("estimated_value", 0)
        backers = airdrop.get("backers", "Unknown")
        community = airdrop.get("community_size", 0)
        complexity = airdrop.get("task_complexity", "unknown").title()
        tasks = ", ".join(airdrop.get("tasks", ["N/A"]))
        chain = airdrop.get("chain", "unknown").title()
        
        # Parse and format end date
        end_date_str = airdrop.get("end_date", "")
        try:
            end_date = datetime.fromisoformat(end_date_str)
            days_left = (end_date - datetime.now()).days
            end_date_display = f"{end_date.strftime('%Y-%m-%d')} ({days_left} days)"
        except:
            end_date_display = "TBD"
        
        risk_label = AirdropAnalyzer.get_risk_label(risk_score)
        
        message = f"""
<b>📊 {name} ({symbol})</b>

<b>Status:</b> {status}
<b>Chain:</b> {chain}
<b>Deadline:</b> {end_date_display}

<b>Description:</b>
{description}

<b>Details:</b>
• Est. Value: ${estimated_value:,.0f}
• Risk Level: {risk_label} ({risk_score:.1%})
• Backers: {backers}
• Community: {community:,}
• Complexity: {complexity}

<b>Required Tasks:</b>
{tasks}

<b>Recommendation:</b>
""".strip()
        
        if risk_score < 0.4:
            message += "\n✅ Good opportunity with manageable risk."
        elif risk_score < 0.7:
            message += "\n⚠️ Moderate risk; research more before committing."
        else:
            message += "\n❌ High risk; recommend caution or skip."
        
        return message


# ============================================================================
# TELEGRAM BOT HANDLERS
# ============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command with welcome message and inline menu."""
    user = update.effective_user
    
    # Add user to database
    db = AirdropDatabase()
    is_new = db.add_user(user.id, user.username)
    
    welcome_msg = (
        f"👋 Welcome, {user.first_name}!\n\n"
        "I'm your crypto airdrop companion. I help you discover, analyze, "
        "and track high-potential airdrops to maximize your earnings.\n\n"
        "What would you like to do?"
    )
    
    keyboard = [
        [
            InlineKeyboardButton("🔥 Latest Airdrops", callback_data="latest"),
            InlineKeyboardButton("🔍 Analyze Token", callback_data="analyze"),
        ],
        [
            InlineKeyboardButton("🔔 Daily Reminders", callback_data="reminders"),
            InlineKeyboardButton("ℹ️ Help", callback_data="help"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(welcome_msg, reply_markup=reply_markup)
    
    if is_new:
        logger.info(f"New user started bot: {user.id} (@{user.username})")


async def latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /latest command to show top 5 newest airdrops."""
    query = update.callback_query
    if query:
        await query.answer()
    
    try:
        api = AirdropAPI()
        airdrops = await api.fetch_airdrops()
        
        # Sort by creation date (newest first) and take top 5
        sorted_airdrops = sorted(
            airdrops,
            key=lambda x: x.get("created_at", ""),
            reverse=True
        )[:5]
        
        if not sorted_airdrops:
            msg = "❌ No airdrops found at the moment. Try again later."
            if query:
                await query.edit_message_text(msg)
            else:
                await update.message.reply_text(msg)
            return
        
        # Build response
        message = "🚀 <b>Top 5 Latest Airdrops</b>\n\n"
        
        for i, airdrop in enumerate(sorted_airdrops, 1):
            name = airdrop.get("name", "Unknown")
            symbol = airdrop.get("symbol", "N/A")
            status = airdrop.get("status", "unknown")
            value = airdrop.get("estimated_value", 0)
            
            risk_score = AirdropAnalyzer.calculate_risk_score(airdrop)
            risk_emoji = "🟢" if risk_score < 0.4 else "🟡" if risk_score < 0.7 else "🔴"
            
            message += f"{i}. <b>{name}</b> ({symbol})\n"
            message += f"   Status: {status.upper()} | Value: ${value:,.0f}\n"
            message += f"   Risk: {risk_emoji} {AirdropAnalyzer.get_risk_label(risk_score)}\n\n"
        
        message += "💡 Use /analyze [TOKEN] to get detailed analysis!"
        
        if query:
            await query.edit_message_text(message, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(message, parse_mode=ParseMode.HTML)
            
    except Exception as e:
        error_msg = f"❌ Error fetching airdrops: {str(e)}"
        logger.error(error_msg)
        if query:
            await query.edit_message_text(error_msg)
        else:
            await update.message.reply_text(error_msg)


async def analyze_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the /analyze conversation to get token input."""
    query = update.callback_query
    if query:
        await query.answer()
        chat_id = query.message.chat_id
    else:
        chat_id = update.message.chat_id
    
    msg = "🔍 <b>Airdrop Analyzer</b>\n\nEnter a token symbol (e.g., OP, ARB, ZRA):"
    
    if query:
        await query.edit_message_text(msg, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    
    return ANALYZE_TOKEN


async def analyze_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process token input and return analysis."""
    user_input = update.message.text.strip().upper()
    
    try:
        api = AirdropAPI()
        airdrops = await api.fetch_airdrops()
        
        # Find matching airdrop
        matching = [a for a in airdrops if a.get("symbol", "").upper() == user_input]
        
        if not matching:
            await update.message.reply_text(
                f"❌ Token '{user_input}' not found. Try: OP, ARB, ZRA, LINEA, SEI"
            )
            return ANALYZE_TOKEN
        
        airdrop = matching[0]
        risk_score = AirdropAnalyzer.calculate_risk_score(airdrop)
        
        # Cache in database
        db = AirdropDatabase()
        db.cache_airdrop(
            airdrop_id=airdrop.get("id", user_input),
            name=airdrop.get("name", ""),
            token_symbol=user_input,
            description=airdrop.get("description", ""),
            status=airdrop.get("status", ""),
            end_date=datetime.fromisoformat(airdrop.get("end_date", datetime.now().isoformat())),
            estimated_value=airdrop.get("estimated_value", 0),
            risk_score=risk_score,
            data=airdrop
        )
        
        # Format and send analysis
        analysis = AirdropAnalyzer.format_airdrop_analysis(airdrop, risk_score)
        await update.message.reply_text(analysis, parse_mode=ParseMode.HTML)
        
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"Error analyzing token {user_input}: {e}")
        await update.message.reply_text(f"❌ Error during analysis: {str(e)}")
        return ConversationHandler.END


async def reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle reminder settings with inline buttons."""
    query = update.callback_query
    if query:
        await query.answer()
    
    db = AirdropDatabase()
    user_id = update.effective_user.id
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Enable Daily Reminders", callback_data="enable_reminders"),
            InlineKeyboardButton("❌ Disable Reminders", callback_data="disable_reminders"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="start")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    msg = (
        "🔔 <b>Daily Reminder Settings</b>\n\n"
        "Receive daily alerts about high-value airdrops ending soon.\n"
        "You'll get notifications about opportunities with low-to-medium risk scores."
    )
    
    if query:
        await query.edit_message_text(msg, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def handle_reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle reminder enable/disable callbacks."""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    db = AirdropDatabase()
    
    if query.data == "enable_reminders":
        db.set_daily_reminders(user_id, True)
        msg = "✅ Daily reminders enabled! You'll receive alerts at 9 AM UTC."
    elif query.data == "disable_reminders":
        db.set_daily_reminders(user_id, False)
        msg = "❌ Daily reminders disabled."
    else:
        return
    
    keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="start")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(msg, reply_markup=reply_markup)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help message."""
    query = update.callback_query
    if query:
        await query.answer()
    
    help_text = """
<b>📖 How to Use This Bot</b>

<b>Commands:</b>
/start - Main menu
/latest - See top 5 newest airdrops
/analyze - Detailed analysis of a specific airdrop
/remind - Manage daily reminder notifications

<b>Risk Scoring (0-100%):</b>
🟢 Low Risk (0-39%): Safe bets with reputable backers
🟡 Medium Risk (40-69%): Balanced risk/reward
🔴 High Risk (70%+): Speculative, requires due diligence

<b>Tips:</b>
• Start with low-risk airdrops if you're new
• Verify project websites before claiming tokens
• Watch for scams asking for wallet keys or seed phrases
• Track your tasks in a spreadsheet for accountability

<b>Disclaimer:</b>
This bot provides analysis for informational purposes only. 
Always do your own research. Crypto carries risk; never invest more than you can afford to lose.
""".strip()
    
    keyboard = [[InlineKeyboardButton("🏠 Home", callback_data="start")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if query:
        await query.edit_message_text(help_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(help_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route button callbacks to appropriate handlers."""
    query = update.callback_query
    
    if query.data == "latest":
        await latest(update, context)
    elif query.data == "analyze":
        return await analyze_start(update, context)
    elif query.data == "reminders":
        await reminders(update, context)
    elif query.data == "help":
        await help_command(update, context)
    elif query.data == "start":
        await start(update, context)
    elif query.data in ["enable_reminders", "disable_reminders"]:
        await handle_reminder_callback(update, context)


# ============================================================================
# SCHEDULED TASKS
# ============================================================================

async def send_daily_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Send daily reminders to opted-in users (scheduled task)."""
    try:
        db = AirdropDatabase()
        users = db.get_reminder_users()
        
        if not users:
            return
        
        # Fetch current airdrops
        api = AirdropAPI()
        airdrops = await api.fetch_airdrops()
        
        # Filter high-value, low-risk airdrops ending in next 7 days
        now = datetime.now()
        upcoming_airdrops = []
        
        for airdrop in airdrops:
            try:
                end_date = datetime.fromisoformat(airdrop.get("end_date", ""))
                days_left = (end_date - now).days
                
                if 0 < days_left <= 7:
                    risk_score = AirdropAnalyzer.calculate_risk_score(airdrop)
                    if risk_score < 0.6:  # Low-to-medium risk
                        upcoming_airdrops.append((airdrop, risk_score, days_left))
            except:
                continue
        
        if not upcoming_airdrops:
            return
        
        # Send to each user
        for user_id in users:
            try:
                message = "🔔 <b>Daily Airdrop Alert</b>\n\nHigh-value opportunities ending soon:\n\n"
                
                for airdrop, risk_score, days_left in upcoming_airdrops[:3]:
                    name = airdrop.get("name", "Unknown")
                    symbol = airdrop.get("symbol", "N/A")
                    value = airdrop.get("estimated_value", 0)
                    risk_label = AirdropAnalyzer.get_risk_label(risk_score)
                    
                    message += f"• <b>{name}</b> ({symbol})\n"
                    message += f"  {days_left} days left | {risk_label}\n"
                    message += f"  Est. value: ${value:,.0f}\n\n"
                
                message += "Use /latest for full details!"
                
                await context.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    parse_mode=ParseMode.HTML
                )
                
                # Mark as notified
                for airdrop, _, _ in upcoming_airdrops:
                    db.mark_user_notified(user_id, airdrop.get("id", ""))
                    
            except Exception as e:
                logger.error(f"Failed to send reminder to user {user_id}: {e}")
    
    except Exception as e:
        logger.error(f"Error in send_daily_reminders: {e}")


# ============================================================================
# MAIN APPLICATION
# ============================================================================

def main():
    """Initialize and run the Telegram bot."""
    if not TELEGRAM_BOT_TOKEN:
        print("❌ ERROR: TELEGRAM_BOT_TOKEN not found in environment variables!")
        print("   Add it to a .env file: TELEGRAM_BOT_TOKEN=your_token_here")
        sys.exit(1)
    
    # Create bot application
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("latest", latest))
    app.add_handler(CommandHandler("help", help_command))
    
    # Analyze conversation
    analyze_handler = ConversationHandler(
        entry_points=[CommandHandler("analyze", analyze_start)],
        states={
            ANALYZE_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, analyze_token)],
        },
        fallbacks=[CommandHandler("start", start)],
    )
    app.add_handler(analyze_handler)
    
    # Reminder handlers
    app.add_handler(CommandHandler("remind", reminders))
    
    # Button callbacks
    app.add_handler(CallbackQueryHandler(button_callback))
    
    # Schedule daily reminders at 9 AM UTC (with error handling)
    try:
        if app.job_queue:
            app.job_queue.run_daily(
                send_daily_reminders,
                time=datetime.strptime("09:00", "%H:%M").time(),
                name="daily_reminders"
            )
            logger.info("Daily reminders scheduled")
    except Exception as e:
        logger.warning(f"Could not schedule daily reminders: {e}. Bot will still work.")
    
    # Start bot
    logger.info("🤖 Airdrop Bot starting...")
    print("\n✅ Bot is running! Send /start to begin.\n")
    
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
