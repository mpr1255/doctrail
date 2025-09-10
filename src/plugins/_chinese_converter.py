"""
Chinese language conversion plugin for doctrail.

This plugin provides conversion utilities for Chinese text,
particularly for location names and administrative divisions.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Try to import pypinyin
try:
    from pypinyin import lazy_pinyin, Style
    PYPINYIN_AVAILABLE = True
except ImportError:
    PYPINYIN_AVAILABLE = False
    logger.warning("pypinyin package not available. Chinese to pinyin conversion will be skipped.")


def chinese_to_pinyin(text: str) -> str:
    """Convert Chinese text to English pinyin format."""
    if not PYPINYIN_AVAILABLE:
        logger.warning("pypinyin not available, skipping Chinese to pinyin conversion")
        return text
    
    if not text or not text.strip():
        return text
    
    clean_text = text.strip()
    
    # Check if text contains Chinese characters
    has_chinese = any('\u4e00' <= char <= '\u9fff' for char in clean_text)
    if not has_chinese:
        return text  # Already in English, return as-is
    
    # Common Chinese place names mapping (for proper capitalization)
    province_mapping = {
        '北京': 'Beijing',
        '天津': 'Tianjin', 
        '河北': 'Hebei',
        '山西': 'Shanxi',
        '内蒙古': 'Inner Mongolia',
        '辽宁': 'Liaoning',
        '吉林': 'Jilin',
        '黑龙江': 'Heilongjiang',
        '上海': 'Shanghai',
        '江苏': 'Jiangsu',
        '浙江': 'Zhejiang',
        '安徽': 'Anhui',
        '福建': 'Fujian',
        '江西': 'Jiangxi',
        '山东': 'Shandong',
        '河南': 'Henan',
        '湖北': 'Hubei',
        '湖南': 'Hunan',
        '广东': 'Guangdong',
        '广西': 'Guangxi',
        '海南': 'Hainan',
        '重庆': 'Chongqing',
        '四川': 'Sichuan',
        '贵州': 'Guizhou',
        '云南': 'Yunnan',
        '西藏': 'Tibet',
        '陕西': 'Shaanxi',
        '甘肃': 'Gansu',
        '青海': 'Qinghai',
        '宁夏': 'Ningxia',
        '新疆': 'Xinjiang',
        '香港': 'Hong Kong',
        '澳门': 'Macau',
        '台湾': 'Taiwan'
    }
    
    # Common Chinese administrative suffixes
    suffixes = {
        '市': 'City',
        '区': 'District', 
        '县': 'County',
        '镇': 'Town',
        '乡': 'Township',
        '村': 'Village',
        '省': 'Province',
        '自治区': 'Autonomous Region',
        '特别行政区': 'Special Administrative Region'
    }
    
    # First check for exact province matches
    if clean_text in province_mapping:
        return province_mapping[clean_text]
    
    # Check for province + suffix combinations
    for chinese_suffix, english_suffix in suffixes.items():
        if clean_text.endswith(chinese_suffix):
            base_name = clean_text[:-len(chinese_suffix)]
            if base_name in province_mapping:
                return f"{province_mapping[base_name]} {english_suffix}"
    
    # First, check for exact suffix matches and replace them
    result_text = clean_text
    for chinese_suffix, english_suffix in suffixes.items():
        if result_text.endswith(chinese_suffix):
            # Replace the suffix with English equivalent
            result_text = result_text[:-len(chinese_suffix)] + ' ' + english_suffix
            break
    
    # If we still have Chinese characters, convert them using pypinyin
    if any('\u4e00' <= char <= '\u9fff' for char in result_text):
        try:
            # Use pypinyin to convert Chinese characters to pinyin
            # Use Style.FIRST_LETTER to avoid tone marks, or Style.NORMAL for basic pinyin
            pinyin_parts = lazy_pinyin(result_text, style=Style.NORMAL)
            
            # Join without spaces first to handle province names correctly
            result_text = ''.join(pinyin_parts)
            
            # Clean up the result and capitalize properly
            result_text = re.sub(r'\s+', ' ', result_text).strip()
            
            # For single words (like province names), just capitalize the first letter
            if ' ' not in result_text:
                result_text = result_text.capitalize()
            else:
                # For multiple words, capitalize each word
                result_text = ' '.join(word.capitalize() for word in result_text.split())
            
        except Exception as e:
            logger.warning(f"Error converting Chinese to pinyin: {e}")
            return text
    
    return result_text


def validate_chinese_language(text: str, field_name: str = "field") -> str:
    """
    Validate that text contains Chinese characters.
    
    Args:
        text: Text to validate
        field_name: Name of the field being validated
        
    Returns:
        The original text if valid
        
    Raises:
        ValueError: If text doesn't contain Chinese characters
    """
    if not text:
        return text
    
    has_chinese = any('\u4e00' <= char <= '\u9fff' for char in text)
    if not has_chinese:
        raise ValueError(f"{field_name} must contain Chinese characters")
    
    return text


def validate_english_language(text: str, field_name: str = "field") -> str:
    """
    Validate that text contains only ASCII characters.
    
    Args:
        text: Text to validate
        field_name: Name of the field being validated
        
    Returns:
        The original text if valid
        
    Raises:
        ValueError: If text contains non-ASCII characters
    """
    if not text:
        return text
    
    if not text.isascii():
        raise ValueError(f"{field_name} must contain only English/ASCII characters")
    
    return text


# Registry of available converters and validators
CONVERTERS = {
    'chinese_to_pinyin': chinese_to_pinyin
}

LANGUAGE_VALIDATORS = {
    'chinese': validate_chinese_language,
    'english': validate_english_language
}