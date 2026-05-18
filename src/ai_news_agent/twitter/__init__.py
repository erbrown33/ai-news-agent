"""
twitter — Twitter/X integration via tweepy.
Traces: SRC-062–SRC-070 (Twitter/X integration), SRC-148 (graceful degradation)
"""

from ai_news_agent.twitter.client import TwitterClient

__all__ = ["TwitterClient"]
