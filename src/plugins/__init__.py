#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///

"""Plugin system for custom ingesters"""

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Dict, Optional, Protocol, List
from abc import abstractmethod
import logging

logger = logging.getLogger(__name__)


class IngesterPlugin(Protocol):
    """Protocol defining the interface for ingester plugins"""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for this plugin"""
        pass
    
    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of what this plugin does"""
        pass
    
    @property
    @abstractmethod
    def target_table(self) -> str:
        """Default table name for ingested data"""
        pass
    
    @abstractmethod
    async def ingest(
        self,
        db_path: str,
        config: Dict,
        verbose: bool = False,
        overwrite: bool = False,
        limit: Optional[int] = None,
        **kwargs
    ) -> Dict[str, int]:
        """
        Main ingestion method.
        
        Args:
            db_path: Path to the target SQLite database
            config: Configuration dictionary from config.yaml
            verbose: Enable verbose logging
            overwrite: Whether to overwrite existing records
            limit: Limit number of records to process
            **kwargs: Additional plugin-specific arguments
            
        Returns:
            Dictionary with ingestion stats (success_count, error_count, etc.)
        """
        pass


def load_plugin(plugin_path: Path) -> Optional[IngesterPlugin]:
    """Load a plugin from a Python file"""
    try:
        # Create module name from file
        module_name = f"doctrail_plugin_{plugin_path.stem}"
        
        # Load the module
        spec = importlib.util.spec_from_file_location(module_name, plugin_path)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            
            # Find the plugin class (should be named Plugin)
            if hasattr(module, 'Plugin'):
                plugin_instance = module.Plugin()
                logger.info(f"Loaded plugin: {plugin_instance.name} from {plugin_path}")
                return plugin_instance
            else:
                logger.error(f"No 'Plugin' class found in {plugin_path}")
                
    except Exception as e:
        logger.error(f"Failed to load plugin from {plugin_path}: {e}")
    
    return None


def discover_plugins(plugin_dir: Optional[Path] = None) -> Dict[str, IngesterPlugin]:
    """Discover all available plugins"""
    plugins = {}
    
    # Default plugin directories
    search_paths = []
    
    # Built-in plugins directory (src/plugins)
    builtin_dir = Path(__file__).parent
    search_paths.append(builtin_dir)
    
    # User plugin directory if specified
    if plugin_dir:
        search_paths.append(plugin_dir)
    
    # Current working directory plugins folder
    cwd_plugins = Path.cwd() / "plugins"
    if cwd_plugins.exists():
        search_paths.append(cwd_plugins)
    
    # Search all paths
    for search_path in search_paths:
        if not search_path.exists():
            continue
            
        for plugin_file in search_path.glob("*.py"):
            if plugin_file.name.startswith("_"):
                continue
                
            plugin = load_plugin(plugin_file)
            if plugin:
                plugins[plugin.name] = plugin
    
    logger.info(f"Discovered {len(plugins)} plugins: {list(plugins.keys())}")
    return plugins


def get_plugin(name: str, plugin_dir: Optional[Path] = None) -> Optional[IngesterPlugin]:
    """Get a specific plugin by name"""
    plugins = discover_plugins(plugin_dir)
    return plugins.get(name) 