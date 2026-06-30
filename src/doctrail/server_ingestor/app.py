"""
FastAPI web application for document ingestion.

This module provides a web interface that wraps the existing ingest functionality
without modifying any of the core ingest logic.
"""

import os
import tempfile
import asyncio
import json
import shutil
import html
import sqlite3
from pathlib import Path
from typing import List, Optional, Dict
import logging

from fastapi import FastAPI, UploadFile, File, Form, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

# Import existing ingest functionality - NO MODIFICATIONS
from ..ingest.core import process_ingest
from ..ingest.database import check_db_schema
from ..db_operations import _quote_identifier


logger = logging.getLogger(__name__)


class ProgressManager:
    """Manages WebSocket connections for progress updates."""
    
    def __init__(self):
        self.connections: List[WebSocket] = []
    
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.connections.append(websocket)
    
    def disconnect(self, websocket: WebSocket):
        if websocket in self.connections:
            self.connections.remove(websocket)
    
    async def send_progress(self, message: str, current: int = 0, total: int = 0, status: str = "progress"):
        if not self.connections:
            return
            
        data = {
            "type": status,
            "message": message,
            "current": current,
            "total": total
        }
        
        # Send to all connected clients
        disconnected = []
        for connection in self.connections:
            try:
                await connection.send_text(json.dumps(data))
            except:
                disconnected.append(connection)
        
        # Clean up disconnected clients
        for connection in disconnected:
            self.disconnect(connection)


def create_app(default_db_path: str = None) -> FastAPI:
    """Create and configure the FastAPI application."""

    # Resolve default database path to an absolute string so the UI shows the real location
    if default_db_path:
        resolved_db_path = str(Path(default_db_path).expanduser().resolve())
    else:
        resolved_db_path = str(Path(os.getcwd()).resolve() / "database.db")

    app = FastAPI(
        title="Doctrail Ingestor",
        description="Web interface for document ingestion",
        version="1.0.0"
    )
    
    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # Store default db path in app state
    app.state.default_db_path = resolved_db_path

    # Create progress manager
    progress_manager = ProgressManager()
    
    # Mount static files (we'll create the directory structure)
    static_path = Path(__file__).parent / "static"
    static_path.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=static_path), name="static")
    
    @app.get("/", response_class=HTMLResponse)
    async def read_root():
        """Serve the main ingestion interface."""
        import time
        cache_buster = str(int(time.time()))  # Add timestamp to prevent caching
        default_path = app.state.default_db_path
        escaped_default_path = html.escape(default_path, quote=True)

        # Build HTML content with default path
        html_content = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <title>Doctrail Ingestor</title>
    <style>
        html, body {
            height: 100%;
            margin: 0;
            padding: 0;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f5f5;
        }
        body {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            box-sizing: border-box;
            min-height: 100vh;
        }
        .container {
            background: white;
            border-radius: 8px;
            padding: 30px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            min-height: calc(100vh - 40px);
            box-sizing: border-box;
        }
        h1 {
            color: #333;
            text-align: center;
            margin-bottom: 30px;
        }
        .form-row {
            display: flex;
            gap: 20px;
            margin-bottom: 20px;
        }
        .form-group {
            flex: 1;
            margin-bottom: 20px;
        }
        .form-group.small {
            flex: 0 0 200px;
        }
        .upload-section {
            border: 2px dashed #ccc;
            border-radius: 8px;
            padding: 40px 20px;
            text-align: center;
            margin-bottom: 30px;
            transition: border-color 0.3s;
        }
        .upload-section.drag-over {
            border-color: #007bff;
            background-color: #f8f9fa;
        }
        label {
            display: block;
            margin-bottom: 5px;
            font-weight: bold;
            color: #555;
        }
        input[type="text"], input[type="file"] {
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            box-sizing: border-box;
        }
        .file-picker-row {
            display: flex;
            gap: 10px;
            align-items: end;
        }
        .file-picker-row input {
            flex: 1;
        }
        .file-picker-row button {
            flex: 0 0 100px;
            padding: 10px 15px;
            background: #6c757d;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
        }
        .file-picker-row button:hover {
            background: #5a6268;
        }
        button {
            background: #007bff;
            color: white;
            border: none;
            padding: 12px 24px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 16px;
        }
        button:hover {
            background: #0056b3;
        }
        button:disabled {
            background: #6c757d;
            cursor: not-allowed;
        }
        .progress-section {
            margin-top: 30px;
            display: none;
        }
        .progress-bar {
            width: 100%;
            height: 20px;
            background: #f0f0f0;
            border-radius: 10px;
            overflow: hidden;
            margin: 10px 0;
        }
        .progress-fill {
            height: 100%;
            background: #28a745;
            width: 0%;
            transition: width 0.3s;
        }
        .log-output {
            background: #f8f9fa;
            border: 1px solid #ddd;
            border-radius: 4px;
            padding: 15px;
            height: 300px;
            overflow-y: auto;
            font-family: 'Courier New', monospace;
            font-size: 14px;
            white-space: pre-wrap;
        }
        .status-message {
            padding: 10px;
            border-radius: 4px;
            margin: 10px 0;
        }
        .status-success {
            background: #d4edda;
            border: 1px solid #c3e6cb;
            color: #155724;
        }
        .status-error {
            background: #f8d7da;
            border: 1px solid #f5c6cb;
            color: #721c24;
        }
        .status-warning {
            background: #fff3cd;
            border: 1px solid #ffeaa7;
            color: #856404;
        }
        .file-list {
            margin: 20px 0;
            padding: 10px;
            background: #f8f9fa;
            border-radius: 4px;
        }
        .cli-command {
            background: #2d3748;
            color: #e2e8f0;
            padding: 15px;
            border-radius: 4px;
            font-family: 'Courier New', monospace;
            margin: 20px 0;
            display: none;
        }
        .cli-command pre {
            margin: 0;
            white-space: pre-wrap;
        }
        .toggle-cli {
            background: #4a5568;
            color: white;
            border: none;
            padding: 8px 12px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 12px;
            margin-bottom: 10px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Doctrail Document Ingestor</h1>
        
        <form id="uploadForm">
            <div class="form-row">
                <div class="form-group">
                    <label for="dbPath">Database Path:</label>
                    <div class="file-picker-row">
                        <input type="text" id="dbPath" name="dbPath" placeholder="./database.db" value="__DEFAULT_DB_PATH__" required>
                        <button type="button" id="dbPathPicker">Browse</button>
                    </div>
                    <input type="file" id="dbPathInput" accept=".db,.sqlite,.sqlite3" style="display: none;">
                    <small style="color: #666; font-size: 12px; margin-top: 5px; display: block;">
                        Use "./" for current directory, or browse to select/create a database file
                    </small>
                </div>
                <div class="form-group small">
                    <label for="tableName">Table Name:</label>
                    <input type="text" id="tableName" name="tableName" value="documents" required>
                </div>
            </div>
            
            <div class="upload-section" id="uploadSection">
                <h3>Drag files or folders here</h3>
                <p>Files and folders can be combined - they won't replace each other</p>
                
                <div style="display: flex; gap: 20px; justify-content: center; margin-top: 20px;">
                    <label for="fileInput" style="background: #007bff; color: white; padding: 10px 20px; border-radius: 4px; cursor: pointer;">
                        Add Files
                        <input type="file" id="fileInput" multiple style="display: none;">
                    </label>
                    
                    <label for="folderInput" style="background: #28a745; color: white; padding: 10px 20px; border-radius: 4px; cursor: pointer;">
                        Add Folder
                        <input type="file" id="folderInput" webkitdirectory multiple style="display: none;">
                    </label>
                </div>
            </div>

            <div class="form-row">
                <div class="form-group">
                    <label for="labels">Labels (comma-separated):</label>
                    <input type="text" id="labels" name="labels" placeholder="policy_docs, source:web">
                    <small style="color: #666; font-size: 12px; margin-top: 5px; display: block;">
                        Stored as a JSON array. Spaces are allowed inside labels.
                    </small>
                </div>
                <div class="form-group">
                    <label for="sourceBase">Source base path (optional):</label>
                    <input type="text" id="sourceBase" name="sourceBase" placeholder="/Users/you/Documents">
                    <small style="color: #666; font-size: 12px; margin-top: 5px; display: block;">
                        If set, file paths stored in DB will be joined as base + relative path.
                    </small>
                </div>
            </div>

            <div id="fileList" class="file-list" style="display: none;">
                <h4>Selected Files: <span id="fileCount">0</span></h4>
                <div id="fileListContent"></div>
            </div>
            
            <button type="button" id="toggleCliBtn" class="toggle-cli">Show CLI Equivalent</button>
            <div id="cliCommand" class="cli-command">
                <pre id="cliCommandText"></pre>
            </div>
            
            <button type="submit" id="submitBtn">Start Ingestion</button>
        </form>
        
        <div id="progressSection" class="progress-section">
            <h3>Progress</h3>
            <div class="progress-bar">
                <div id="progressFill" class="progress-fill"></div>
            </div>
            <div id="progressText">Ready...</div>
            
            <h4>Log Output:</h4>
            <div id="logOutput" class="log-output"></div>
        </div>
        
        <div id="statusMessage" style="display: none;"></div>
    </div>

    <script>
        let websocket = null;
        // Map unique key -> { file, relativePath, originalPath }
        let allFiles = new Map();
        
        // File handling
        const fileInput = document.getElementById('fileInput');
        const folderInput = document.getElementById('folderInput');
        const uploadSection = document.getElementById('uploadSection');
        const fileList = document.getElementById('fileList');
        const fileListContent = document.getElementById('fileListContent');
        const fileCount = document.getElementById('fileCount');
        
        // Database path picker
        const dbPathInput = document.getElementById('dbPathInput');
        const dbPathPicker = document.getElementById('dbPathPicker');
        const dbPath = document.getElementById('dbPath');
        
        dbPathPicker.addEventListener('click', async () => {
            // For browsers that support the File System Access API (Chrome 86+)
            if ('showSaveFilePicker' in window) {
                try {
                    const currentPath = dbPath.value.trim();
                    let suggestedName = 'database.db';
                    
                    // Extract filename from current path to use as suggestion
                    if (currentPath) {
                        const pathParts = currentPath.replace(/^\.\//, '').split('/');
                        suggestedName = pathParts[pathParts.length - 1] || 'database.db';
                    }
                    
                    const fileHandle = await window.showSaveFilePicker({
                        suggestedName: suggestedName,
                        types: [{
                            description: 'Database files',
                            accept: {
                                'application/x-sqlite3': ['.db', '.sqlite', '.sqlite3'],
                            },
                        }],
                    });
                    
                    // For security, browsers only give us the filename, not full path
                    // So we'll prefix with ./ to indicate current directory
                    dbPath.value = './' + fileHandle.name;
                    updateCliCommand();
                    
                } catch (err) {
                    // User cancelled or API not supported
                    if (err.name !== 'AbortError') {
                        console.log('File System Access API not supported, falling back to file input');
                    }
                    // Fall back to the old file input method with better messaging
                    showBrowseFallback();
                }
            } else {
                // Fallback for browsers without File System Access API
                showBrowseFallback();
            }
        });
        
        function showBrowseFallback() {
            // Show a helpful message about the limitation
            const helpText = document.createElement('div');
            helpText.style.cssText = `
                position: fixed; top: 20px; right: 20px; 
                background: #fff3cd; border: 1px solid #ffeaa7; 
                padding: 10px; border-radius: 4px; max-width: 300px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1); z-index: 1000;
                font-size: 12px; color: #856404;
            `;
            helpText.innerHTML = `
                <strong>Browser limitation:</strong><br>
                File picker will open in default location. You can:
                <br>• Select existing .db file to use that location
                <br>• Type the path directly (e.g., ./my-project/data.db)
                <br>• Use "./" for current directory
            `;
            document.body.appendChild(helpText);
            
            // Remove help text after 5 seconds
            setTimeout(() => {
                if (helpText.parentNode) {
                    helpText.parentNode.removeChild(helpText);
                }
            }, 5000);
            
            // Trigger the file picker
            dbPathInput.click();
        }
        
        dbPathInput.addEventListener('change', (e) => {
            if (e.target.files && e.target.files[0]) {
                const file = e.target.files[0];
                // Extract just the filename and make it relative to current directory
                dbPath.value = './' + file.name;
                updateCliCommand();
            }
        });
        
        // Auto-resolve database path to absolute
        dbPath.addEventListener('blur', () => {
            const path = dbPath.value.trim();
            if (path && !path.startsWith('/')) {
                // For demo - in real app you'd send to backend for resolution
                if (path.startsWith('./')) {
                    dbPath.value = path; // Keep as is for now
                } else if (!path.includes('/')) {
                    dbPath.value = './' + path; // Prepend ./
                }
            }
        });
        
        function buildFileKey(file, relativePath, originalPath) {
            if (originalPath && originalPath.length > 0) {
                return originalPath;
            }
            if (relativePath && relativePath.length > 0) {
                return relativePath;
            }
            return `${file.name}-${file.lastModified}-${file.size}`;
        }

        function addFilesToCollection(files) {
            Array.from(files).forEach(file => {
                const relativePath = (file.webkitRelativePath && file.webkitRelativePath.length > 0)
                    ? file.webkitRelativePath
                    : file.name;
                const originalPath = (file.path && file.path.length > 0)
                    ? file.path
                    : relativePath;
                const key = buildFileKey(file, relativePath, originalPath);

                allFiles.set(key, {
                    file,
                    relativePath,
                    originalPath
                });

                console.log('File:', file.name, 'relativePath:', relativePath, 'originalPath:', originalPath, 'size:', file.size);
            });
            updateFileList();
            updateCliCommand();
        }
        
        function updateFileList() {
            const entries = Array.from(allFiles.values());
            fileCount.textContent = entries.length;
            
            if (entries.length === 0) {
                fileList.style.display = 'none';
                return;
            }
            
            fileList.style.display = 'block';
            
            const totalSize = entries.reduce((sum, entry) => sum + entry.file.size, 0);
            const totalSizeMB = (totalSize / (1024 * 1024)).toFixed(1);
            
            let content = `<div style="margin-bottom: 10px;"><strong>Total: ${entries.length} files (${totalSizeMB} MB)</strong></div>`;
            
            // Simple approach: just show the files as they are
            entries.slice(0, 15).forEach(entry => {
                const size = (entry.file.size / 1024).toFixed(1);
                const displayName = entry.originalPath || entry.relativePath || entry.file.name;
                const icon = (entry.relativePath && entry.relativePath.includes('/')) ? '[dir]' : '[file]';
                content += `<div>${icon} ${displayName} (${size} KB)</div>`;
            });
            
            if (entries.length > 15) {
                content += `<div style="color: #666; font-style: italic;">... and ${entries.length - 15} more files</div>`;
            }
            
            fileListContent.innerHTML = content;
        }
        
        function updateCliCommand() {
            const db = dbPath.value || './database.db';
            const table = document.getElementById('tableName').value || 'documents';
            const labels = (document.getElementById('labels').value || '').split(',').map(s => s.trim()).filter(Boolean);
            const entries = Array.from(allFiles.values());
            
            if (entries.length === 0) {
                document.getElementById('cliCommandText').textContent = 'No files selected';
                return;
            }
            
            let command = `./doctrail.py ingest --db-path "${db}" --table "${table}" --input-dir "<temp-directory>"`;
            if (labels.length > 0) {
                for (const lab of labels) {
                    const safeLab = lab.replace(/"/g, '\"');
                    command += ` \
  --label "${safeLab}"`;
                }
            }

            const sourceBase = (document.getElementById('sourceBase').value || '').trim();
            if (sourceBase) {
                command += `
# Stored file paths will be resolved relative to: ${sourceBase}`;
            }

            command += `

# Web interface processes ${entries.length} files total`;
            command += `
# Files are temporarily staged and then ingested`;

            document.getElementById('cliCommandText').textContent = command;
        }
        
        fileInput.addEventListener('change', (e) => {
            addFilesToCollection(e.target.files);
        });
        
        folderInput.addEventListener('change', (e) => {
            addFilesToCollection(e.target.files);
        });
        
        // CLI command toggle
        document.getElementById('toggleCliBtn').addEventListener('click', () => {
            const cliCommand = document.getElementById('cliCommand');
            const btn = document.getElementById('toggleCliBtn');
            if (cliCommand.style.display === 'none' || !cliCommand.style.display) {
                cliCommand.style.display = 'block';
                btn.textContent = 'Hide CLI Equivalent';
            } else {
                cliCommand.style.display = 'none';
                btn.textContent = 'Show CLI Equivalent';
            }
        });
        
        // Drag and drop
        uploadSection.addEventListener('dragover', (e) => {
            e.preventDefault();
            uploadSection.classList.add('drag-over');
        });
        
        uploadSection.addEventListener('dragleave', (e) => {
            e.preventDefault();
            uploadSection.classList.remove('drag-over');
        });
        
        // Helper function to recursively read directory contents
        async function readDirectory(directoryEntry, path = '') {
            const files = [];
            const reader = directoryEntry.createReader();
            
            return new Promise((resolve) => {
                const readEntries = () => {
                    reader.readEntries(async (entries) => {
                        if (entries.length === 0) {
                            resolve(files);
                            return;
                        }
                        
                        for (const entry of entries) {
                            const fullPath = path + '/' + entry.name;
                            if (entry.isDirectory) {
                                // Recursively read subdirectory
                                const subFiles = await readDirectory(entry, fullPath);
                                files.push(...subFiles);
                            } else {
                                // It's a file, get it
                                await new Promise((resolveFile) => {
                                    entry.file((file) => {
                                        // Set the relative path for proper display
                                        // Don't substring if path doesn't start with /
                                        const relativePath = fullPath.startsWith('/') ? fullPath.substring(1) : fullPath;
                                        Object.defineProperty(file, 'webkitRelativePath', {
                                            value: relativePath,
                                            writable: false
                                        });
                                        files.push(file);
                                        resolveFile();
                                    });
                                });
                            }
                        }
                        
                        // Continue reading entries (directories may have many files)
                        readEntries();
                    });
                };
                
                readEntries();
            });
        }
        
        uploadSection.addEventListener('drop', async (e) => {
            e.preventDefault();
            uploadSection.classList.remove('drag-over');
            
            const items = e.dataTransfer.items;
            if (!items) {
                // Old browser fallback
                addFilesToCollection(e.dataTransfer.files);
                return;
            }
            
            const allFiles = [];
            
            for (let i = 0; i < items.length; i++) {
                const item = items[i];
                if (item.kind === 'file') {
                    const entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
                    
                    if (entry) {
                        if (entry.isDirectory) {
                            // Read directory contents recursively
                            console.log('Reading directory:', entry.name);
                            const dirFiles = await readDirectory(entry, entry.name);
                            allFiles.push(...dirFiles);
                        } else {
                            // Regular file
                            const file = item.getAsFile();
                            if (file) {
                                allFiles.push(file);
                            }
                        }
                    } else {
                        // No entry API, just get as file
                        const file = item.getAsFile();
                        if (file) {
                            allFiles.push(file);
                        }
                    }
                }
            }
            
            if (allFiles.length > 0) {
                addFilesToCollection(allFiles);
            }
        });
        
        // WebSocket connection
        function connectWebSocket() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            websocket = new WebSocket(`${protocol}//${window.location.host}/ws`);
            
            websocket.onmessage = function(event) {
                const data = JSON.parse(event.data);
                updateProgress(data);
            };
            
            websocket.onclose = function() {
                console.log('WebSocket connection closed');
            };
        }
        
        function updateProgress(data) {
            const progressSection = document.getElementById('progressSection');
            const progressFill = document.getElementById('progressFill');
            const progressText = document.getElementById('progressText');
            const logOutput = document.getElementById('logOutput');
            const statusMessage = document.getElementById('statusMessage');
            
            progressSection.style.display = 'block';
            
            // Always add to log
            logOutput.textContent += data.message + String.fromCharCode(10);
            logOutput.scrollTop = logOutput.scrollHeight;
            
            if (data.type === 'progress') {
                const percentage = data.total > 0 ? (data.current / data.total) * 100 : 0;
                progressFill.style.width = percentage + '%';
                if (data.total > 0) {
                    progressText.textContent = `${data.message} (${data.current}/${data.total})`;
                } else {
                    progressText.textContent = data.message;
                }
            } else if (data.type === 'success') {
                progressFill.style.width = '100%';
                statusMessage.className = 'status-message status-success';
                statusMessage.textContent = data.message;
                statusMessage.style.display = 'block';
                
                document.getElementById('submitBtn').disabled = false;
                document.getElementById('submitBtn').textContent = 'Start Ingestion';
            } else if (data.type === 'warning') {
                statusMessage.className = 'status-message status-warning';
                statusMessage.textContent = data.message;
                statusMessage.style.display = 'block';
            } else if (data.type === 'info') {
                // Just log info messages, don't change status
            } else if (data.type === 'error') {
                statusMessage.className = 'status-message status-error';
                statusMessage.textContent = data.message;
                statusMessage.style.display = 'block';
                
                document.getElementById('submitBtn').disabled = false;
                document.getElementById('submitBtn').textContent = 'Start Ingestion';
            }
        }
        
        // Form submission
        document.getElementById('uploadForm').addEventListener('submit', async function(e) {
            e.preventDefault();
            
            const entries = Array.from(allFiles.values());
            if (entries.length === 0) {
                alert('Please select files or folders to ingest');
                return;
            }
            
            const submitBtn = document.getElementById('submitBtn');
            submitBtn.disabled = true;
            submitBtn.textContent = 'Processing...';
            
            // Clear previous status
            document.getElementById('statusMessage').style.display = 'none';
            document.getElementById('logOutput').textContent = '';
            document.getElementById('progressFill').style.width = '0%';
            
            // Connect WebSocket for progress updates
            connectWebSocket();
            
            const formData = new FormData();
            formData.append('db_path', document.getElementById('dbPath').value);
            formData.append('table_name', document.getElementById('tableName').value);
            formData.append('labels', document.getElementById('labels').value || '');
            formData.append('source_base', document.getElementById('sourceBase').value || '');

            const manifest = [];

            // Add all files; preserve relative paths by overriding filename when available
            entries.forEach(entry => {
                const { file, relativePath, originalPath } = entry;
                const rel = relativePath || file.name;
                formData.append('files', file, rel);
                manifest.push({
                    relative_path: rel,
                    original_path: originalPath || rel
                });
            });

            formData.append('file_manifest', JSON.stringify(manifest));
            
            try {
                const response = await fetch('/ingest', {
                    method: 'POST',
                    body: formData
                });
                
                if (!response.ok) {
                    const errorText = await response.text();
                    throw new Error(`HTTP ${response.status}: ${errorText}`);
                }
                
                const result = await response.json();
                console.log('Upload completed:', result);
                
            } catch (error) {
                console.error('Upload failed:', error);
                updateProgress({
                    type: 'error',
                    message: `Upload failed: ${error.message}`
                });
            }
        });
        
        // Initialize
        document.addEventListener('DOMContentLoaded', async function() {
            const currentValue = (dbPath.value || '').trim();

            // If CLI provided a default path, keep it intact
            if (currentValue && currentValue !== './database.db') {
                updateCliCommand();
                return;
            }

            // Otherwise fall back to CWD so users see where the DB will live
            try {
                const response = await fetch('/get-cwd');
                const data = await response.json();
                dbPath.value = data.cwd + '/database.db';
            } catch (e) {
                dbPath.value = currentValue || './database.db';
            }
            updateCliCommand();
        });
    </script>
</body>
</html>
        """
        html_content = html_content.replace("__DEFAULT_DB_PATH__", escaped_default_path)
        return HTMLResponse(content=html_content)
    
    @app.get("/get-cwd")
    async def get_cwd():
        """Return the current working directory."""
        return {"cwd": os.getcwd()}
    
    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint for progress updates."""
        await progress_manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            progress_manager.disconnect(websocket)
    
    @app.post("/ingest")
    async def ingest_documents(
        db_path: str = Form(...),
        table_name: str = Form(default="documents"),
        files: List[UploadFile] = File(...),
        labels: Optional[str] = Form(default=""),
        source_base: Optional[str] = Form(default=""),
        file_manifest: Optional[str] = Form(default=None)
    ):
        """
        Handle document ingestion via file upload.
        
        This is a pure wrapper around the existing process_ingest function.
        """
        if not files:
            raise HTTPException(status_code=400, detail="No files uploaded")
        
        # Expand and resolve database path to absolute
        db_path = os.path.expanduser(db_path)
        db_path = os.path.abspath(db_path)
        table_name_ref = _quote_identifier(table_name, "table name")
        
        # Validate database directory
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            try:
                os.makedirs(db_dir, exist_ok=True)
                await progress_manager.send_progress(f"Created database directory: {db_dir}")
            except Exception as e:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Cannot create database directory {db_dir}: {str(e)}"
                )
        
        await progress_manager.send_progress(f"Starting ingestion of {len(files)} files to {db_path}...")

        manifest_entries: List[Dict[str, str]] = []
        if file_manifest:
            try:
                parsed = json.loads(file_manifest)
                if isinstance(parsed, list):
                    manifest_entries = [entry for entry in parsed if isinstance(entry, dict)]
                else:
                    logger.warning("File manifest payload is not a list; ignoring")
            except Exception as e:
                logger.warning(f"Failed to parse file_manifest: {e}")
        
        # Use context manager to ensure cleanup
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                await progress_manager.send_progress(f"Preparing files in temporary directory: {temp_dir}")
                
                # Save uploaded files, preserving folder structure
                saved_files = []
                path_map = {}
                for i, file in enumerate(files):
                    try:
                        manifest_entry = manifest_entries[i] if i < len(manifest_entries) else {}

                        # Determine relative path preserved from the browser submission
                        relative_candidate = None
                        if manifest_entry:
                            relative_candidate = manifest_entry.get('relative_path') or manifest_entry.get('relativePath')
                        if not relative_candidate:
                            relative_candidate = getattr(file, 'filename', file.filename)

                        if not relative_candidate:
                            relative_candidate = file.filename or f"uploaded_file_{i}"

                        if not isinstance(relative_candidate, str):
                            relative_candidate = str(relative_candidate)

                        # Normalise and sanitize the relative path to keep folder structure but avoid traversal
                        relative_candidate = relative_candidate.replace('\\', '/').strip()
                        relative_candidate = relative_candidate.lstrip('/').lstrip('./')
                        relative_parts = [part for part in Path(relative_candidate).parts if part not in ('', '.', '..')]
                        if relative_parts:
                            relative_path = str(Path(*relative_parts))
                        else:
                            relative_path = Path(relative_candidate).name or f"uploaded_file_{i}"

                        # Create the full path in temp directory
                        file_path = Path(temp_dir) / relative_path
                        file_path.parent.mkdir(parents=True, exist_ok=True)
                        
                        # Save file content
                        content = await file.read()
                        with open(file_path, 'wb') as f:
                            f.write(content)
                        
                        saved_files.append(str(file_path))

                        # Determine the best original path hint to persist
                        original_path_hint = None
                        if manifest_entry:
                            maybe_original = manifest_entry.get('original_path') or manifest_entry.get('originalPath')
                            if isinstance(maybe_original, str) and maybe_original.strip():
                                original_path_hint = maybe_original.strip()

                        resolved_original_path = None
                        if original_path_hint:
                            resolved_original_path = original_path_hint
                            if source_base and source_base.strip() and not os.path.isabs(resolved_original_path):
                                try:
                                    resolved_original_path = os.path.normpath(os.path.join(source_base.strip(), resolved_original_path))
                                except Exception:
                                    logger.debug("Could not combine source_base with original path hint; keeping hint as-is")
                        elif source_base and source_base.strip():
                            try:
                                resolved_original_path = os.path.normpath(os.path.join(source_base.strip(), relative_path))
                            except Exception:
                                resolved_original_path = relative_path
                        else:
                            resolved_original_path = relative_path

                        path_map[str(file_path)] = resolved_original_path
                        
                        # Progress update
                        if (i + 1) % 10 == 0 or i == len(files) - 1:
                            await progress_manager.send_progress(
                                f"Preparing {i + 1}/{len(files)} files for processing", 
                                current=i + 1, 
                                total=len(files)
                            )
                        
                    except Exception as e:
                        logger.error(f"Error saving file {file.filename}: {e}")
                        raise HTTPException(
                            status_code=500, 
                            detail=f"Failed to save file {file.filename}: {str(e)}"
                        )
                
                await progress_manager.send_progress("All files prepared. Starting document ingestion...")
                
                # Track only files that may become documents (ignore sidecars like .json)
                candidate_files = [
                    path for path in saved_files
                    if os.path.isfile(path) and Path(path).suffix.lower() != '.json'
                ]
                sidecar_files = [path for path in saved_files if Path(path).suffix.lower() == '.json']

                await progress_manager.send_progress(
                    f"Found {len(candidate_files)} files in temp directory to process"
                )
                if sidecar_files:
                    await progress_manager.send_progress(
                        f"Detected {len(sidecar_files)} sidecar metadata files",
                        status="info"
                    )
                
                initial_doc_count = 0
                try:
                    with sqlite3.connect(db_path) as conn:
                        cursor = conn.cursor()
                        cursor.execute(f"SELECT COUNT(*) FROM {table_name_ref}")
                        initial_doc_count = cursor.fetchone()[0]
                except Exception:
                    pass  # Table might not exist yet
                
                # Call the existing process_ingest function - NO MODIFICATIONS
                # This is the PURE WRAPPER - just calling the existing CLI logic
                try:
                    result = await process_ingest(
                        db_path=db_path,
                        input_dir=temp_dir,
                        table=table_name,
                        verbose=True,
                        force=False,
                        overwrite=False,
                        limit=None,
                        include_pattern=None,
                        exclude_pattern=None,
                        readability=False,
                        html_extractor='default',
                        skip_garbage_check=False,
                        yes=True,  # Auto-confirm
                        fulltext=False,
                        manifest_path=None,
                        labels=[l.strip() for l in (labels.split(',') if labels else []) if l.strip()],
                        override_filepaths=path_map
                    )
                    
                    # Query the database to see what was actually added
                    final_doc_count = 0
                    try:
                        with sqlite3.connect(db_path) as conn:
                            cursor = conn.cursor()
                            cursor.execute(f"SELECT COUNT(*) FROM {table_name_ref}")
                            final_doc_count = cursor.fetchone()[0]
                    except Exception as e:
                        logger.error(f"Could not query database: {e}")
                    
                    # Calculate what actually happened
                    docs_added = final_doc_count - initial_doc_count
                    docs_not_added = max(0, len(candidate_files) - docs_added)
                    
                    # Try to get more detail by checking for duplicates
                    duplicate_count = 0
                    try:
                        # Get SHA1 hashes of all files we tried to add
                        import hashlib
                        attempted_sha1s = []
                        for filepath in candidate_files:
                            if os.path.isfile(filepath) and os.path.getsize(filepath) > 0:
                                with open(filepath, 'rb') as f:
                                    sha1 = hashlib.sha1(f.read()).hexdigest()
                                    attempted_sha1s.append(sha1)
                        
                        # Check how many already existed
                        if attempted_sha1s:
                            with sqlite3.connect(db_path) as conn:
                                cursor = conn.cursor()
                                placeholders = ','.join(['?'] * len(attempted_sha1s))
                                cursor.execute(
                                    f"SELECT COUNT(*) FROM {table_name_ref} WHERE sha1 IN ({placeholders})",
                                    attempted_sha1s,
                                )
                                total_matching = cursor.fetchone()[0]
                                # Duplicates = files that existed before we added new ones
                                duplicate_count = max(0, total_matching - docs_added)
                    except Exception as e:
                        logger.debug(f"Could not check for duplicates: {e}")
                    
                    failed_count = max(0, docs_not_added - duplicate_count)
                    
                    # Send accurate status
                    if docs_not_added > 0:
                        await progress_manager.send_progress(
                            f"Ingestion summary (check terminal for details):",
                            status="warning"
                        )
                        await progress_manager.send_progress(
                            f"  • Files uploaded to browser: {len(files)}",
                            status="info"
                        )
                        await progress_manager.send_progress(
                            f"  • Actual files to process: {len(candidate_files)}",
                            status="info"
                        )
                        await progress_manager.send_progress(
                            f"  • Successfully added to DB: {docs_added}",
                            status="success"
                        )
                        if duplicate_count > 0:
                            await progress_manager.send_progress(
                                f"  • Duplicates (already in DB): {duplicate_count}",
                                status="info"
                            )
                        if failed_count > 0:
                            await progress_manager.send_progress(
                                f"  • Failed with errors: {failed_count}",
                                status="error"
                            )
                            await progress_manager.send_progress(
                                f"    See terminal output below for specific error details",
                                status="warning"
                            )
                        await progress_manager.send_progress(
                            f"  • Total docs now in DB: {final_doc_count}",
                            status="info"
                        )
                    else:
                        await progress_manager.send_progress(
                            f"Success. Added {docs_added} new documents",
                            status="success"
                        )
                        await progress_manager.send_progress(
                            f"Database now contains {final_doc_count} total documents",
                            status="success"
                        )
                    
                    return {
                        "status": "success",
                        "message": "Documents ingested successfully",
                        "db_path": db_path,
                        "table_name": table_name,
                        "files_processed": len(files),
                        "temp_directory": temp_dir,  # For transparency
                        "cli_equivalent": f"./doctrail.py ingest --db-path '{db_path}' --table '{table_name}' --input-dir '<your-files>'"
                    }
                    
                except Exception as e:
                    error_msg = f"Ingestion failed: {str(e)}"
                    await progress_manager.send_progress(error_msg, status="error")
                    raise HTTPException(status_code=500, detail=error_msg)
                    
                # Note: TemporaryDirectory context manager automatically cleans up temp_dir
                # when this block exits, so no manual cleanup needed!
                    
        except Exception as e:
            error_msg = f"File processing failed: {str(e)}"
            await progress_manager.send_progress(error_msg, status="error")
            raise HTTPException(status_code=500, detail=error_msg)
    
    return app


def start_server(host: str = "127.0.0.1", port: int = 8000, db_path: str = None):
    """Start the FastAPI server."""
    import uvicorn

    app = create_app(default_db_path=db_path)
    display_db_path = getattr(app.state, "default_db_path", None)
    
    print(f"Starting Doctrail Ingestor Web Interface")
    print(f"📍 Server running at: http://{host}:{port}")
    if display_db_path:
        print(f"📂 Default database: {display_db_path}")
    print(f"Open your browser and drag files to ingest.")
    print(f"🗂️  This is a PURE WRAPPER around: ./doctrail.py ingest")
    print(f"🧹 Temporary files are automatically cleaned up after processing")
    
    uvicorn.run(app, host=host, port=port)
