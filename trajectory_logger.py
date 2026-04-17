import sqlite3
import os
import json
import logging
from datetime import datetime

logger = logging.getLogger("astrbot")

class TrajectoryLogger:
    """Manages RL Trajectories storage using SQLite."""
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS episodes (
                        session_id TEXT PRIMARY KEY,
                        instruction TEXT,
                        final_status TEXT,
                        reason TEXT,
                        total_steps INTEGER DEFAULT 0,
                        reward INTEGER DEFAULT 0,
                        timestamp TEXT
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS steps (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT,
                        step_index INTEGER,
                        screenshot_path TEXT,
                        think_content TEXT,
                        action_content TEXT,
                        reward INTEGER DEFAULT 0,
                        timestamp TEXT,
                        FOREIGN KEY(session_id) REFERENCES episodes(session_id)
                    )
                ''')
                conn.commit()
        except Exception as e:
            logger.error(f"[TrajectoryLogger] Failed to init db: {e}")

    def start_episode(self, session_id: str, instruction: str):
        """Starts a new episode marking the initial goal."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO episodes (session_id, instruction, timestamp)
                    VALUES (?, ?, ?)
                ''', (session_id, instruction, datetime.now().isoformat()))
                conn.commit()
        except Exception as e:
            logger.error(f"[TrajectoryLogger] Failed to start episode {session_id}: {e}")

    def log_step(self, session_id: str, step_index: int, screenshot_path: str, think_content: str, action_content: str):
        """Logs a single step within an episode."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                # Parse action if needed, or store as json
                if isinstance(action_content, dict):
                    action_content = json.dumps(action_content, ensure_ascii=False)
                    
                cursor.execute('''
                    INSERT INTO steps (session_id, step_index, screenshot_path, think_content, action_content, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (session_id, step_index, screenshot_path, think_content, action_content, datetime.now().isoformat()))
                conn.commit()
                # logger.debug(f"[TrajectoryLogger] Step {step_index} logged for {session_id}.")
        except Exception as e:
            logger.error(f"[TrajectoryLogger] Failed to log step for {session_id}: {e}")

    def commit_episode(self, session_id: str, final_status: str, reason: str, reward: int = 0):
        """Finalizes an episode, updating total steps and reward."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                # Calculate total steps for this episode
                cursor.execute('SELECT COUNT(*) FROM steps WHERE session_id = ?', (session_id,))
                total_steps = cursor.fetchone()[0]

                cursor.execute('''
                    UPDATE episodes
                    SET final_status = ?, reason = ?, total_steps = ?, reward = ?
                    WHERE session_id = ?
                ''', (final_status, reason, total_steps, reward, session_id))
                conn.commit()
                logger.info(f"[TrajectoryLogger] Episode {session_id} committed. Status: {final_status}, Steps: {total_steps}, Reward: {reward}")
        except Exception as e:
            logger.error(f"[TrajectoryLogger] Failed to commit episode {session_id}: {e}")
