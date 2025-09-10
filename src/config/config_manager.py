"""Centralized configuration management."""

import os
import yaml
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List

from ..types import ConfigDict, ModelConfig, EnrichmentConfig
from ..constants import DEFAULT_TABLE_NAME, DEFAULT_MODEL
from .validators import ConfigValidator


class ConfigManager:
    """Manages application configuration."""
    
    def __init__(self, config_path: Optional[str] = None):
        """Initialize configuration manager.
        
        Args:
            config_path: Optional path to configuration file
        """
        self.config_path = config_path
        self._config: ConfigDict = {}
        self._validator = ConfigValidator()
        self.logger = logging.getLogger(__name__)
        
        if config_path:
            self.load_config(config_path)
    
    def load_config(self, config_path: str) -> ConfigDict:
        """Load configuration from YAML file.
        
        Args:
            config_path: Path to YAML configuration file
            
        Returns:
            Loaded configuration dictionary
            
        Raises:
            FileNotFoundError: If config file doesn't exist
            yaml.YAMLError: If YAML is invalid
            ValueError: If configuration is invalid
        """
        config_path = Path(config_path).resolve()
        
        if not config_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {config_path}")
        
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                self._config = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise yaml.YAMLError(f"Invalid YAML in config file: {e}")
        
        # Store config path for relative path resolution
        self._config['__config_path__'] = str(config_path)
        
        # Validate configuration
        errors = self._validator.validate(self._config)
        if errors:
            raise ValueError(f"Configuration errors: {', '.join(errors)}")
        
        # Apply defaults
        self._apply_defaults()
        
        self.logger.info(f"Loaded configuration from {config_path}")
        return self._config
    
    def _apply_defaults(self) -> None:
        """Apply default values to configuration."""
        # Set default table if not specified
        if 'default_table' not in self._config:
            self._config['default_table'] = DEFAULT_TABLE_NAME
        
        # Set default model if not specified
        if 'default_model' not in self._config:
            self._config['default_model'] = DEFAULT_MODEL
        
        # Ensure sections exist
        for section in ['sql_queries', 'models', 'enrichments', 'exports']:
            if section not in self._config:
                self._config[section] = {}
    
    @property
    def database_path(self) -> str:
        """Get database path from configuration."""
        db_path = self._config.get('database', '')
        return os.path.expanduser(db_path)
    
    @property
    def default_table(self) -> str:
        """Get default table name."""
        return self._config.get('default_table', DEFAULT_TABLE_NAME)
    
    @property
    def default_model(self) -> str:
        """Get default model name."""
        return self._config.get('default_model', DEFAULT_MODEL)
    
    def get_sql_query(self, name: str) -> Optional[str]:
        """Get SQL query by name.
        
        Args:
            name: Query name
            
        Returns:
            SQL query string or None
        """
        return self._config.get('sql_queries', {}).get(name)
    
    def get_model_config(self, name: str) -> Optional[ModelConfig]:
        """Get model configuration by name.
        
        Args:
            name: Model name
            
        Returns:
            Model configuration or None
        """
        model_data = self._config.get('models', {}).get(name)
        if model_data:
            return ModelConfig(
                name=model_data.get('name', name),
                max_tokens=model_data.get('max_tokens', 4096),
                temperature=model_data.get('temperature', 0.1)
            )
        return None
    
    def get_enrichment(self, name: str) -> Optional[EnrichmentConfig]:
        """Get enrichment configuration by name.
        
        Args:
            name: Enrichment name
            
        Returns:
            Enrichment configuration or None
        """
        enrichments = self._config.get('enrichments', [])
        for enrichment in enrichments:
            if enrichment.get('name') == name:
                return enrichment
        return None
    
    def get_enrichments(self) -> List[EnrichmentConfig]:
        """Get all enrichment configurations.
        
        Returns:
            List of enrichment configurations
        """
        return self._config.get('enrichments', [])
    
    def get_export_config(self, name: str) -> Optional[Dict[str, Any]]:
        """Get export configuration by name.
        
        Args:
            name: Export name
            
        Returns:
            Export configuration or None
        """
        return self._config.get('exports', {}).get(name)
    
    def resolve_path(self, path: str) -> Path:
        """Resolve a path relative to config file.
        
        Args:
            path: Path to resolve
            
        Returns:
            Resolved absolute path
        """
        if os.path.isabs(path):
            return Path(path)
        
        if '__config_path__' in self._config:
            config_dir = Path(self._config['__config_path__']).parent
            return config_dir / path
        
        return Path(path).resolve()
    
    def update(self, updates: Dict[str, Any]) -> None:
        """Update configuration with new values.
        
        Args:
            updates: Dictionary of updates to apply
        """
        self._config.update(updates)
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value by key.
        
        Args:
            key: Configuration key
            default: Default value if key not found
            
        Returns:
            Configuration value
        """
        return self._config.get(key, default)
    
    def __getitem__(self, key: str) -> Any:
        """Get configuration value by key using dict syntax."""
        return self._config[key]
    
    def __contains__(self, key: str) -> bool:
        """Check if key exists in configuration."""
        return key in self._config
    
    @property
    def raw_config(self) -> ConfigDict:
        """Get raw configuration dictionary."""
        return self._config