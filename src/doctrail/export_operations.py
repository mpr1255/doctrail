import os
import json
import logging
from pathlib import Path
from typing import List, Dict
import yaml
from jinja2 import Template
import subprocess
from .db_operations import execute_query

def create_markdown_document(row: Dict, config: Dict, export_name: str) -> str:
    """Generate a markdown document from database row and template configuration"""
    
    # Get export configuration
    export_config = config['exports'][export_name]
    template_config = export_config['template_config']
    template_path = Path(__file__).parent / template_config['template']
    
    with open(template_path) as f:
        template = Template(f.read())
    
    # Only log full document details if debug level
    sha1 = row.get('sha1', 'unknown')
    logging.debug(f"\n{'='*50}\nProcessing document: {sha1}")
    logging.debug(f"Raw row data: {json.dumps(row, indent=2, ensure_ascii=False)}")
    
    try:
        zh_json = json.loads(row.get('zh_json') or '{}')
        en_json = json.loads(row.get('en_json') or '{}')
        
        english_translation = row.get('english_translation')
        if english_translation:
            try:
                english_translation = json.loads(english_translation)
            except (json.JSONDecodeError, TypeError):
                english_translation = {}
        else:
            english_translation = {}
        
        logging.debug(f"Parsed zh_json ({len(zh_json)} lines): {json.dumps(zh_json, indent=2, ensure_ascii=False)}")
        logging.debug(f"Parsed en_json ({len(en_json)} lines): {json.dumps(en_json, indent=2, ensure_ascii=False)}")
        
        if not zh_json or not en_json:
            logging.warning(f"Empty translation data for row {sha1}")
            
    except json.JSONDecodeError as e:
        logging.error(f"JSON parse error for {sha1}: {e}")
        logging.error(f"Raw zh_json: {row.get('zh_json')}")
        logging.error(f"Raw en_json: {row.get('en_json')}")
        zh_json = {}
        en_json = {}
    
    # Ensure all template variables have defaults
    template_vars = {
        'sha1': row.get('sha1', 'unknown'),
        'title': row.get('title', 'No Title'),
        'zh_lines': zh_json,
        'en_lines': en_json,
        'styling': template_config['styling']
    }
    
    # Render document with defaults
    doc = template.render(**template_vars)
    
    # Only log rendered document in debug
    logging.debug(f"Rendered document:\n{doc}")
    
    return doc

def get_output_filename(row: Dict, naming_pattern: str, fallback_pattern: str) -> str:
    """Generate filename based on pattern with fallback"""
    try:
        # Try the main pattern first, using any fields from the row
        return naming_pattern.format(**row)
    except (KeyError, ValueError):
        # Fall back to simpler pattern
        return fallback_pattern.format(
            sha1=row.get('sha1', 'unknown')
        )

def export_documents(db_path: str, config: Dict, output_dir: str, export_name: str) -> None:
    """Export documents based on configuration"""
    
    # Get export configuration
    if 'exports' not in config or export_name not in config['exports']:
        raise ValueError(f"Export configuration '{export_name}' not found")
    
    export_config = config['exports'][export_name]
    
    # Expand and create output directory
    output_dir = os.path.expanduser(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    logging.info(f"Exporting to directory: {output_dir}")
    
    # Get naming patterns
    naming_pattern = export_config.get('output_naming', 
                                     config.get('output_naming', {}).get('default', '{sha1}'))
    fallback_pattern = config.get('output_naming', {}).get('fallback', 'doc_{sha1}')
    
    # Validate required fields
    required_fields = ['query', 'formats', 'template']
    missing = [f for f in required_fields if f not in export_config]
    if missing:
        raise ValueError(f"Export '{export_name}' missing required fields: {', '.join(missing)}")
    
    # Get rows from database that have required fields
    results = execute_query(db_path, export_config['query'])
    logging.info(f"Retrieved {len(results)} rows from database")
    
    # Filter out rows with empty required fields
    required_data_fields = export_config.get('required_fields', [])
    filtered_results = [
        row for row in results 
        if all(row.get(field) and row[field].strip() for field in required_data_fields)
    ]
    logging.info(f"Filtered to {len(filtered_results)} non-empty rows")
    
    template_name = export_config['template']
    formats = export_config['formats']
    
    for row in filtered_results:
        title = row.get('title', 'No Title')
        logging.info(f"Processing: {title}")
        
        # Generate base filename using sha1 as it should always exist
        base_name = get_output_filename(row, naming_pattern, fallback_pattern)
        
        # Create markdown document
        md_content = create_markdown_document(row, config, export_name)
        
        # Write markdown file
        md_path = Path(output_dir) / f"{base_name}.md"
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(md_content)
        
        # Convert to requested formats using pandoc
        for fmt in formats:
            try:
                output_path = Path(output_dir) / f"{base_name}.{fmt}"
                
                # Basic pandoc command
                cmd = [
                    'pandoc',
                    str(md_path),
                    '-o', str(output_path),
                    '--standalone',
                    '--metadata', f'title={title}'
                ]
                
                # Format-specific options
                if fmt == 'pdf':
                    cmd.extend(['-t', 'typst'])
                elif fmt == 'html':
                    cmd.extend(['--embed-resources'])
                
                logging.debug(f"Running pandoc command: {' '.join(cmd)}")
                result = subprocess.run(cmd, 
                                     check=True,
                                     capture_output=True,
                                     text=True,
                                     encoding='utf-8')
                
                if result.stderr:
                    logging.warning(f"Pandoc warnings for {title}: {result.stderr}")
                    
                logging.info(f"Generated {fmt.upper()} for: {title}")
                
            except subprocess.CalledProcessError as e:
                logging.error(f"Failed to generate {fmt.upper()} for: {title}")
                logging.error(f"Pandoc error: {e.stderr}")