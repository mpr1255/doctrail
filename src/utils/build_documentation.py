#!/usr/bin/env -S uv run
"""
Unified documentation builder for doctrail.

This tool consolidates README building and documentation extraction
to ensure documentation stays in sync with the actual implementation.
"""
import yaml
import ast
import re
import click
from pathlib import Path
from typing import Dict, List, Set


def extract_cli_commands(main_py_path: Path) -> Dict[str, str]:
    """Extract CLI commands and their help text from main.py"""
    with open(main_py_path, 'r') as f:
        content = f.read()
    
    # Find all @cli.command() decorators and their associated functions
    commands = {}
    command_pattern = r'@cli\.command\(\)\s*\n((?:@click\.[^\n]+\n)*)s*def\s+(\w+)\([^)]*\):\s*\n\s*"""([^"]+)"""'
    
    for match in re.finditer(command_pattern, content, re.MULTILINE):
        decorators, cmd_name, docstring = match.groups()
        commands[cmd_name] = {
            'help': docstring.strip(),
            'options': extract_click_options(decorators)
        }
    
    return commands


def extract_click_options(decorators: str) -> List[Dict[str, str]]:
    """Extract click options from decorator strings"""
    options = []
    option_pattern = r"@click\.option\('([^']+)'(?:,\s*'([^']+)')?,\s*([^)]+)\)"
    
    for match in re.finditer(option_pattern, decorators):
        short, long_opt, params = match.groups()
        option_info = {'flags': short}
        if long_opt:
            option_info['flags'] += f", {long_opt}"
        
        # Extract help text
        help_match = re.search(r"help='([^']+)'", params)
        if help_match:
            option_info['help'] = help_match.group(1)
        
        options.append(option_info)
    
    return options


def extract_schema_types(schema_managers_path: Path) -> Set[str]:
    """Extract supported schema types from schema_managers.py"""
    with open(schema_managers_path, 'r') as f:
        content = f.read()
    
    # Look for JSON Schema type definitions
    types = set()
    type_pattern = r"type['\"]:\s*['\"](\w+)['\"]"
    
    for match in re.finditer(type_pattern, content):
        types.add(match.group(1))
    
    return types


def extract_model_limits(llm_ops_path: Path) -> Dict[str, int]:
    """Extract model context limits from llm_operations.py"""
    with open(llm_ops_path, 'r') as f:
        content = f.read()
    
    # Find MODEL_CONTEXT_LIMITS dictionary
    limits = {}
    limits_pattern = r"MODEL_CONTEXT_LIMITS\s*=\s*{([^}]+)}"
    
    match = re.search(limits_pattern, content, re.DOTALL)
    if match:
        limits_str = match.group(1)
        # Extract individual model limits
        model_pattern = r"'([^']+)':\s*(\d+)"
        for m in re.finditer(model_pattern, limits_str):
            model, limit = m.groups()
            limits[model] = int(limit)
    
    return limits


def load_yaml_section(file_path: str, start_marker: str, end_marker: str = None, max_lines: int = 30) -> str:
    """Load a section of a YAML file between markers or up to max_lines."""
    with open(file_path, 'r') as f:
        lines = f.readlines()
    
    result = []
    in_section = False
    line_count = 0
    
    for line in lines:
        if start_marker in line:
            in_section = True
        
        if in_section:
            result.append(line.rstrip())
            line_count += 1
            
            if end_marker and end_marker in line:
                break
            if line_count >= max_lines:
                result.append("    # ... (truncated for brevity)")
                break
    
    return '\n'.join(result)


def extract_config_examples(examples_path: Path) -> Dict[str, Dict]:
    """Extract configuration examples from YAML files"""
    examples = {}
    
    for yaml_file in examples_path.glob("*.yml"):
        if yaml_file.name.startswith("test_"):
            continue
            
        try:
            with open(yaml_file, 'r') as f:
                config = yaml.safe_load(f)
            
            if config and 'enrichments' in config:
                # Extract first enrichment as example
                first_enrichment = config['enrichments'][0] if config['enrichments'] else None
                if first_enrichment:
                    examples[yaml_file.name] = {
                        'name': first_enrichment.get('name', 'unnamed'),
                        'description': first_enrichment.get('description', 'No description'),
                        'has_schema': 'schema' in first_enrichment,
                        'has_append_file': 'append_file' in first_enrichment,
                        'output_table': first_enrichment.get('output_table')
                    }
        except Exception as e:
            print(f"⚠️  Error reading {yaml_file.name}: {e}")
    
    return examples


def build_readme():
    """Build README.md from template and live examples."""
    
    # Get the project root directory (parent of src)
    project_root = Path(__file__).parent.parent
    
    # Load the enum schema example
    enum_example = load_yaml_section(
        project_root / 'examples/enum_schema_demo.yml',
        start_marker='enrichments:',
        max_lines=25
    )
    
    # Load a more complex example
    complex_example = load_yaml_section(
        project_root / 'examples/comprehensive_example.yml',
        start_marker='# Step 4: Comprehensive analysis',
        end_marker='model: gpt-4o',
        max_lines=20
    )
    
    # Read the current README as template
    readme_path = project_root / 'README.md'
    if readme_path.exists():
        with open(readme_path, 'r') as f:
            readme_content = f.read()
    else:
        readme_content = ""
    
    # Define the new examples section
    examples_section = f'''## Configuration examples

### Inline enum schema

```yaml
{enum_example}
```

### Complex schema with external rubric

```yaml
{complex_example}
```

For complete examples, see:
- `examples/enum_schema_demo.yml` - Demonstrates all enum schema features
- `examples/comprehensive_example.yml` - Complex multi-step workflow
- `tests/test_config_full.yml` - Comprehensive test configuration
'''
    
    # Replace the examples section in README
    import re
    
    # Pattern to match existing examples section (case insensitive for the heading)
    pattern = r'## [Cc]onfiguration [Ee]xamples.*?(?=##|$)'
    
    if re.search(pattern, readme_content, re.DOTALL):
        # Replace existing section
        new_readme = re.sub(pattern, examples_section + '\n', readme_content, flags=re.DOTALL)
    else:
        # Append if not found
        new_readme = readme_content + '\n\n' + examples_section
    
    # Write back
    with open(readme_path, 'w') as f:
        f.write(new_readme)
    
    print("✅ README.md updated with live examples")


@click.command()
@click.option('--readme', is_flag=True, help='Update README.md with examples')
@click.option('--docs', is_flag=True, help='Extract documentation from code')
@click.option('--all', is_flag=True, help='Update everything')
@click.option('--check', is_flag=True, help='Check documentation currency without updating')
def build(readme: bool, docs: bool, all: bool, check: bool):
    """Build/update documentation from source code."""
    
    # Get project paths
    project_root = Path(__file__).parent.parent
    src_path = project_root / "src"
    docs_path = project_root / "docs"
    examples_path = project_root / "examples"
    
    if all or readme:
        print("📝 Updating README.md...")
        build_readme()
    
    if all or docs:
        print("🔍 Extracting information from source code...")
        
        # Extract various information
        commands = extract_cli_commands(src_path / "main.py")
        schema_types = extract_schema_types(src_path / "schema_managers.py")
        model_limits = extract_model_limits(src_path / "llm_operations.py")
        config_examples = extract_config_examples(examples_path)
        
        if check:
            print("\n✅ Documentation check complete. Run without --check to update.")
            return
        
        # Report extracted information
        print("\n📋 CLI Commands Found:")
        for cmd, info in commands.items():
            print(f"   - {cmd}: {info['help']}")
            if info['options']:
                for opt in info['options']:
                    print(f"     {opt['flags']}: {opt.get('help', 'No help text')}")
        
        print("\n📊 Schema Types Found:")
        for t in sorted(schema_types):
            print(f"   - {t}")
        
        print("\n🤖 Model Context Limits:")
        for model, limit in sorted(model_limits.items()):
            print(f"   - {model}: {limit:,} tokens")
        
        print("\n📚 Configuration Examples Found:")
        for filename, info in config_examples.items():
            features = []
            if info['has_schema']:
                features.append("schema-driven")
            if info['has_append_file']:
                features.append("append_file")
            if info['output_table']:
                features.append(f"output_table={info['output_table']}")
            
            features_str = f" ({', '.join(features)})" if features else ""
            print(f"   - {filename}: {info['description']}{features_str}")
    
    if not any([readme, docs, all]):
        print("Please specify --readme, --docs, or --all")
        return
    
    print("\n✅ Documentation build complete!")
    if all or docs:
        print("\n💡 Next steps:")
        print("   1. Review the extracted information above")
        print("   2. Consider adding more automated doc generation")


if __name__ == '__main__':
    build()