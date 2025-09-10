"""Fix for update_output_table to handle null values properly"""

def update_output_table_fixed(db_path: str, output_table: str, key_column: str, key_value: str, output_data: dict, 
                       enrichment_id: str = None, model_used: str = None):
    """Update or insert data into a separate output table ONLY if there's meaningful data.
    
    This version filters out:
    1. Rows where all values are null/None
    2. Rows where values are the string "null"
    3. Rows where the primary evidence field is empty
    """
    import logging
    import json
    from datetime import datetime
    
    # Check if there's any meaningful data
    has_data = False
    cleaned_data = {}
    
    for col, val in output_data.items():
        # Skip null-like values
        if val is None or val == "null" or val == "":
            continue
            
        # For lists/dicts, check if they're empty
        if isinstance(val, (list, dict)) and not val:
            continue
            
        # If we get here, we have some data
        has_data = True
        cleaned_data[col] = val
    
    # Don't insert if no meaningful data
    if not has_data:
        logging.debug(f"Skipping insert for {key_column}={key_value} - no meaningful data")
        return
    
    # For cash_payments specifically, check if evidence_zh exists
    if output_table == 'cash_payments' and not cleaned_data.get('evidence_zh'):
        logging.debug(f"Skipping insert for {key_column}={key_value} - no evidence found")
        return
    
    # Rest of the function continues with cleaned_data instead of output_data...
    # [Original update logic here with cleaned_data]