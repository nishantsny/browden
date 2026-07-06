import logging
import sys

def configure_logging():
    """Configure logging to stderr for MCP compatibility."""
    # Create a logger for the package
    logger = logging.getLogger("browden")
    
    # Avoid duplicate handlers if called multiple times
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        
        # Standard formatter: [LEVEL] name: message
        formatter = logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
        
        # Explicitly stream to stderr
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        
        # Ensure it doesn't propagate to root logger which might write to stdout
        logger.propagate = False

    return logger

# Global logger instance for the package
logger = configure_logging()
