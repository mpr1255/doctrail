"""Central constants for Doctrail application."""

from typing import Set, Dict

# Concurrency limits
DEFAULT_API_SEMAPHORE_LIMIT = 30  # Maximum concurrent API calls
DEFAULT_DB_SEMAPHORE_LIMIT = 2    # Maximum concurrent database writes

# Batch processing
DEFAULT_BATCH_SIZE = 100
MAX_BATCH_SIZE = 1000

# Database settings
DEFAULT_BUSY_TIMEOUT = 30.0  # SQLite busy timeout in seconds
DEFAULT_TABLE_NAME = "documents"
DEFAULT_KEY_COLUMN = "sha1"

# Model defaults
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TEMPERATURE = 0.1
DEFAULT_MAX_TOKENS = 4096

# File types
SUPPORTED_FILE_EXTENSIONS: Set[str] = {
    # Documents
    '.pdf', '.txt', '.doc', '.docx', '.odt', '.rtf',
    # Web
    '.html', '.htm', '.mhtml', '.mht', '.xml',
    # Ebooks
    '.epub', '.mobi', '.azw', '.azw3', '.fb2', '.djvu',
    # Academic
    '.tex', '.bib',
    # Data
    '.csv', '.json', '.jsonl',
    # Markdown
    '.md', '.markdown', '.mdown', '.mkd',
    # Code
    '.py', '.js', '.java', '.cpp', '.c', '.h', '.cs', '.rb', '.go', '.rs',
    '.php', '.swift', '.kt', '.scala', '.r', '.m', '.sql', '.sh', '.yaml', '.yml',
    # Archives (for extraction)
    '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z',
    # Other
    '.log', '.org', '.rst', '.adoc', '.textile'
}

# Skip patterns
SKIP_PATTERNS: Set[str] = {
    '.DS_Store', 'Thumbs.db', 'desktop.ini', '.gitignore',
    '.dockerignore', 'package-lock.json', 'yarn.lock',
    'poetry.lock', 'Gemfile.lock', '__pycache__', '.pyc'
}

# Directory skip patterns
SKIP_DIRECTORIES: Set[str] = {
    '.git', '.svn', '.hg', '__pycache__', 'node_modules',
    '.idea', '.vscode', '.vs', 'venv', 'env', '.env',
    'build', 'dist', 'target', '.pytest_cache', '.mypy_cache'
}

# Hardcoded enrichment names (to be refactored)
TRANSLATION_ENRICHMENTS: Set[str] = {
    'translate_to_english',
    'translate_to_english_by_line'
}

# Logging
LOG_FILE_PATH = "/tmp/doctrail.log"
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Progress display
PROGRESS_BAR_FORMAT = '{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]'
SPINNER_CHARS = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']

# Text extraction quality thresholds
MIN_TEXT_LENGTH = 10
GARBAGE_THRESHOLD = 0.5  # Ratio of non-printable chars
MAX_EXTRACTION_ATTEMPTS = 3

# API retry settings
MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY_BASE = 2  # Base delay in seconds for exponential backoff

# Export formats
SUPPORTED_EXPORT_FORMATS: Set[str] = {
    'csv', 'json', 'jsonl', 'md', 'markdown', 
    'html', 'pdf', 'docx', 'txt'
}

# Environment variables
ENV_OPENAI_API_KEY = "OPENAI_API_KEY"
ENV_GEMINI_API_KEY = "GOOGLE_API_KEY"
ENV_ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"

# Error messages
ERROR_NO_ENRICHMENTS = "Config file must have an 'enrichments' section"
ERROR_NO_DATABASE = "Config file must specify 'database' path"
ERROR_ENRICHMENT_NOT_FOUND = "No matching enrichments found"
ERROR_TABLE_NOT_FOUND = "Table '{table}' doesn't exist"
ERROR_COLUMN_NOT_FOUND = "Column '{column}' doesn't exist"

# Success messages
SUCCESS_INGESTION = "✅ Ingestion complete: {processed} documents processed"
SUCCESS_ENRICHMENT = "✅ Enrichment '{name}' complete: {processed} rows processed"
SUCCESS_EXPORT = "✅ Export complete: {output_path}"