import sys
import os
import subprocess
import threading
import json
import time
import base64
import requests # Direct HTTP
import google.auth.transport.requests
from google.oauth2 import service_account

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QLabel, QFileDialog, QScrollArea, 
                             QGridLayout, QMessageBox, QProgressBar, QTabWidget,
                             QRadioButton, QButtonGroup, QLineEdit, QFormLayout, QFrame,
                             QCheckBox, QGroupBox, QDialog, QComboBox, QTextEdit, QSizePolicy,
                             QListWidget, QListWidgetItem, QInputDialog, QTableWidget, 
                             QTableWidgetItem, QHeaderView, QAbstractItemView, QSlider, QSpinBox)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QEvent, QSize, QTimer
from PyQt6.QtGui import QPixmap, QFont, QKeyEvent, QIcon
import shutil
import uuid

DEFAULT_KEY_PATH = os.path.join(os.getcwd(), "aivideowear-85d19890ba52.json")
API_ENDPOINT_TEMPLATE = "https://us-central1-aiplatform.googleapis.com/v1/projects/{PROJECT_ID}/locations/us-central1/publishers/google/models/{MODEL_ID}:predict"



# --- CUSTOM WIDGETS ---
class ClickableLabel(QLabel):
    clicked = pyqtSignal()
    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)

class SelectableImageWidget(QWidget):
    def __init__(self, image_path, parent=None):
        super().__init__(parent)
        self.image_path = image_path
        self.selected = False
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        
        self.thumb_label = ClickableLabel()
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_label.setStyleSheet("border: 1px solid #555;")
        self.thumb_label.setCursor(Qt.CursorShape.PointingHandCursor)
        
        pix = QPixmap(image_path)
        if not pix.isNull():
             self.thumb_label.setPixmap(pix.scaled(160, 90, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        
        layout.addWidget(self.thumb_label)
        self.checkbox = QCheckBox("Select")
        layout.addWidget(self.checkbox)
        self.checkbox.toggled.connect(self.on_toggle)

    def on_toggle(self, checked):
        self.selected = checked

    def set_timestamp(self, text):
        # Create label if not exists
        if not hasattr(self, 'lbl_time'):
            self.lbl_time = QLabel(text)
            self.lbl_time.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.lbl_time.setStyleSheet("color: #00E676; font-size: 11px; font-weight: bold;")
            self.layout().insertWidget(2, self.lbl_time) # Insert before checkbox (index 2)
        else:
            self.lbl_time.setText(text)

class LightboxDialog(QDialog):
    def __init__(self, image_paths, current_index, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Lightbox Viewer")
        self.showFullScreen()
        self.image_paths = image_paths
        self.current_index = current_index
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("background-color: black;")
        layout.addWidget(self.image_label)
        controls = QHBoxLayout()
        controls.setContentsMargins(20, 20, 20, 20)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.close)
        btn_close.setFixedWidth(100)
        self.lbl_counter = QLabel()
        self.lbl_counter.setStyleSheet("color: white; font-weight: bold;")
        controls.addWidget(self.lbl_counter)
        controls.addStretch()
        controls.addWidget(btn_close)
        layout.addLayout(controls)
        self.load_image()

    def load_image(self):
        if 0 <= self.current_index < len(self.image_paths):
            path = self.image_paths[self.current_index]
            pix = QPixmap(path)
            self.image_label.setPixmap(pix.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            self.lbl_counter.setText(f"{self.current_index + 1} / {len(self.image_paths)}")

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Left:
            self.current_index = max(0, self.current_index - 1)
            self.load_image()
        elif event.key() == Qt.Key.Key_Right:
            self.current_index = min(len(self.image_paths) - 1, self.current_index + 1)
            self.load_image()
        elif event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

# --- WORKERS ---

class FFmpegWorker(QThread):
    finished = pyqtSignal(bool, str, str) 
    def __init__(self, command, task_type):
        super().__init__()
        self.command = command
        self.task_type = task_type 
    def run(self):
        try:
            process = subprocess.run(self.command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **get_subprocess_kwargs())
            if process.returncode == 0:
                # SUCCESS: Emit stderr too because metadata=print goes there
                self.finished.emit(True, "Operation Successful", process.stderr)
            else:
                self.finished.emit(False, process.stderr, "")
        except Exception as e:
            self.finished.emit(False, str(e), "")



class AssemblyWorker(QThread):
    progress_signal = pyqtSignal(int, int, str)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, clip_data, output_path, mix_settings=None):
        super().__init__()
        self.clip_data = clip_data 
        self.output_path = output_path
        self.mix_settings = mix_settings # {enabled, original_path, generated_vol}
        self.temp_dir = os.path.join(os.path.dirname(output_path), "temp_assembly")

    def run(self):
        try:
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
            os.makedirs(self.temp_dir)

            processed_clips = []
            total = len(self.clip_data)

            # 1. Process Clips
            # Transition Settings
            transition = self.mix_settings.get("transition", "None")
            fade_dur = 0.5 # Duration for fade in/out

            for i, clip in enumerate(self.clip_data):
                self.progress_signal.emit(i+1, total + 2, f"Processing clip {i+1}/{total}...")
                
                input_video = clip.get('video')
                input_image = clip.get('image') # New for fallback
                target_dur = clip['target_dur']
                source_dur = clip.get('source_dur', 0)
                
                chunk_out = os.path.join(self.temp_dir, f"chunk_{i:04d}.mp4")
                
                # --- FADE FILTERS (Common Logic) ---
                # Fade In (Start of clip) and Fade Out (End of clip)
                # Video Fade: fade=t=in:st=0:d=0.5,fade=t=out:st={dur-0.5}:d=0.5
                # Audio Fade: afade=t=in:ss=0:d=0.5,afade=t=out:st={dur-0.5}:d=0.5
                
                v_fade = ""
                a_fade = ""
                
                if transition in ["Fade Black", "Fade White"]:
                    color = "black" if transition == "Fade Black" else "white"
                    st_out = max(0, target_dur - fade_dur)
                    
                    # Video Fades
                    v_fade = f",fade=t=in:st=0:d={fade_dur}:color={color}"
                    v_fade += f",fade=t=out:st={st_out}:d={fade_dur}:color={color}"
                    
                    # Audio Fades (Only if video input exists)
                    if input_video:
                         a_fade = f",afade=t=in:ss=0:d={fade_dur}"
                         a_fade += f",afade=t=out:st={st_out}:d={fade_dur}"
                
                if input_image and not input_video:
                    # --- ZOOM GENERATION ---
                    # Logic: Create a video from image with Zoom
                    zoom_amt = self.mix_settings.get("zoom_amount", 110)
                    zoom_factor = zoom_amt / 100.0
                    
                    # duration in frames (approx 30fps)
                    d_frames = int(target_dur * 30)
                    
                    # Zoompan filter:
                    # z='1+((1.1-1)*(on/duration))' -> linear zoom from 1.0 to 1.1
                    z_expr = f"1+({zoom_factor}-1)*(on/{d_frames})"
                    
                    # Add FADE to zoompan? No, chain it.
                    # Note: zoompan resets timestamps, better to chain fade after.
                    
                    # Zoom Filter (Supersampled to reduce jitter)
                    # We render at 2560x1440 (2x) then scale down to smooth the movement.
                    zoom_filter = f"zoompan=z='{z_expr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={d_frames}:s=2560x1440:fps=30,scale=1280:720"
                    
                    # Combine Filters: Zoom -> Fade
                    full_v_filter = f"[0:v]{zoom_filter}{v_fade}[v]"
                    
                    cmd = [
                        "ffmpeg", "-loop", "1", "-i", input_image,
                        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100:duration={target_dur}",
                        "-filter_complex", full_v_filter,
                        "-map", "[v]", "-map", "1:a",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-t", str(target_dur),
                        "-y", chunk_out
                    ]
                    
                    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                elif input_video:
                    # --- VIDEO SPEED ADJUST ---
                    # Speed Calculation
                    speed = source_dur / target_dur
                    speed = max(0.1, min(speed, 100.0))

                    # Filters
                    video_filter = f"setpts=PTS/{speed}"
                    # Append Fade Video
                    video_filter += v_fade
                    
                    audio_chain = []
                    remaining_speed = speed
                    while remaining_speed > 2.0:
                        audio_chain.append("atempo=2.0")
                        remaining_speed /= 2.0
                    while remaining_speed < 0.5:
                        audio_chain.append("atempo=0.5")
                        remaining_speed /= 0.5
                    audio_chain.append(f"atempo={remaining_speed}")
                    
                    audio_filter = ",".join(audio_chain)
                    # Append Fade Audio
                    audio_filter += a_fade
                    
                    # Scale/Pad
                    video_filter += ",scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30"

                    cmd = [
                        "ffmpeg", "-i", input_video, 
                        "-filter:v", video_filter,
                        "-filter:a", audio_filter,
                        "-c:v", "libx264", "-c:a", "aac", 
                        "-y", chunk_out
                    ]
                    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                if os.path.exists(chunk_out):
                    processed_clips.append(chunk_out)

            # 2. Concat
            self.progress_signal.emit(total + 1, total + 2, "Concatenating...")
            concat_list_path = os.path.join(self.temp_dir, "list.txt")
            with open(concat_list_path, "w") as f:
                for p in processed_clips:
                    f.write(f"file '{p}'\n")

            temp_assembly = os.path.join(self.temp_dir, "temp_full.mp4")
            
            cmd_concat = [
                "ffmpeg", "-f", "concat", "-safe", "0", 
                "-i", concat_list_path, 
                "-c", "copy", "-y", temp_assembly
            ]
            subprocess.run(cmd_concat, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # 3. Audio Mixing (Optional)
            final_target = self.output_path
            
            if self.mix_settings and self.mix_settings.get("enabled"):
                self.progress_signal.emit(total + 2, total + 2, "Mixing Audio...")
                
                original_vid = self.mix_settings.get("original_path")
                gen_vol = self.mix_settings.get("generated_vol", 0.25)
                
                final_mix_output = temp_assembly # Default if mixing fails or not needed
                
                if original_vid and os.path.exists(original_vid):
                    # Check if generated video has audio stream
                    has_gen_audio = False
                    try:
                        probe_cmd = ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type", "-of", "csv=p=0", temp_assembly]
                        p_res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, text=True)
                        if p_res.stdout.strip(): has_gen_audio = True
                    except: pass

                    mix_temp = os.path.join(self.temp_dir, "mixed_temp.mp4")
                    
                    if has_gen_audio:
                        # Mix Both: [0:a]volume={gen_vol}[a0];[1:a]volume=1.0[a1];[a0][a1]amix...
                        filter_complex = f"[0:a]volume={gen_vol:.2f}[a0];[1:a]volume=1.0[a1];[a0][a1]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
                        cmd_mix = [
                            "ffmpeg", "-i", temp_assembly, "-i", original_vid,
                            "-filter_complex", filter_complex,
                            "-map", "0:v:0", "-map", "[aout]",
                            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                            "-shortest", "-y", mix_temp
                        ]
                    else:
                        # No generated audio: Pass original audio through (100% vol)
                        # We just map video from 0 and audio from 1
                        cmd_mix = [
                            "ffmpeg", "-i", temp_assembly, "-i", original_vid,
                            "-map", "0:v:0", "-map", "1:a:0",
                            "-c:v", "copy", "-c:a", "copy",
                            "-shortest", "-y", mix_temp
                        ]

                    subprocess.run(cmd_mix, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    
                    if os.path.exists(mix_temp):
                        final_mix_output = mix_temp
                    else:
                         print("DEBUG: Mix failed, using temp_assembly")
            else:
                 final_mix_output = temp_assembly

            # 4. Audio Normalization (Optional)
            if self.mix_settings and self.mix_settings.get("audio_norm"):
                self.progress_signal.emit(total + 2, total + 2, "Normalizing Audio (Loudnorm)...")
                # loudnorm=I=-16:TP=-1.5:LRA=11
                
                norm_temp = os.path.join(self.temp_dir, "normalized.mp4")
                
                cmd_norm = [
                    "ffmpeg", "-i", final_mix_output,
                    "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k",
                    "-y", norm_temp
                ]
                subprocess.run(cmd_norm, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                if os.path.exists(norm_temp):
                    shutil.move(norm_temp, final_target)
                else:
                    # Fallback to mix output if norm failed
                     shutil.move(final_mix_output, final_target)
            else:
                # No normalization: Just move mixed output
                shutil.move(final_mix_output, final_target)

            self.finished_signal.emit(True, final_target)

        except Exception as e:
            import traceback
            traceback.print_exc()
            self.finished_signal.emit(False, str(e))

class ClipTableWidget(QTableWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragEnabled(True) # Allow internal drag
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        
        self.setColumnCount(6)
        self.setHorizontalHeaderLabels(["Frame", "Timestamp", "Target Dur", "Assigned Video", "Source Dur", "Action"])
        self.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.setIconSize(QSize(100, 56))
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)

    def dragEnterEvent(self, event):
        # Always accept Drag
        event.accept()

    def dragMoveEvent(self, event):
        event.accept()

    def dropEvent(self, event):
        # 1. External Files (Upload)
        if event.mimeData().hasUrls():
            files = [u.toLocalFile() for u in event.mimeData().urls()]
            video_files = [f for f in files if f.lower().endswith(('.mp4', '.mov', '.avi'))]
            
            row = self.rowAt(event.position().toPoint().y())
            if row >= 0 and video_files:
                self.set_video_for_row(row, video_files[0])
                # Save State Hook (Parent)
                self.notify_change()
            event.accept()
            return

        # 2. Internal Move (Swap Rows)
        source = event.source()
        if source == self:
            rows = sorted(set(index.row() for index in self.selectedIndexes()))
            if not rows: return
            
            target_row = self.rowAt(event.position().toPoint().y())
            if target_row == -1: return
            src_row = rows[0]
            
            if src_row == target_row: return
            
            self.swap_rows(src_row, target_row)
            self.notify_change()
            event.accept()

    def swap_rows(self, row1, row2):
        # We only swap the Video (3) and Duration (4) columns
        # Use takeItem to preserve Icon and Data without regeneration
        
        # Take items from Row 1
        vid1 = self.takeItem(row1, 3)
        dur1 = self.takeItem(row1, 4)
        
        # Take items from Row 2
        vid2 = self.takeItem(row2, 3)
        dur2 = self.takeItem(row2, 4)
        
        # Place Items Swapped
        # Handle None cases just in case (though usually populated)
        if vid2: self.setItem(row1, 3, vid2)
        else: self.set_video_for_row(row1, None) # Recreate empty if needed
            
        if dur2: self.setItem(row1, 4, dur2)
        else: self.setItem(row1, 4, QTableWidgetItem("-"))

        if vid1: self.setItem(row2, 3, vid1)
        else: self.set_video_for_row(row2, None)
            
        if dur1: self.setItem(row2, 4, dur1)
        else: self.setItem(row2, 4, QTableWidgetItem("-"))

    def set_video_for_row(self, row, video_path):
        fname = os.path.basename(video_path) if video_path else "Drop Video Here"
        item = QTableWidgetItem(fname)
        
        if video_path and os.path.exists(video_path):
            item.setData(Qt.ItemDataRole.UserRole, video_path)
            # Generate Thumbnail
            try:
                temp_thumb = os.path.join(os.path.dirname(video_path), f".thumb_{uuid.uuid4().hex[:8]}.jpg")
                # Fast seek to 1s, scale to height 56 (matching icon size)
                cmd = [
                    "ffmpeg", "-ss", "00:00:01", "-i", video_path, 
                    "-vframes", "1", "-vf", "scale=-1:56", 
                    "-y", temp_thumb
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **get_subprocess_kwargs())
                
                if os.path.exists(temp_thumb):
                    pix = QPixmap(temp_thumb) 
                    if not pix.isNull():
                        item.setIcon(QIcon(pix))
                    try: os.remove(temp_thumb)
                    except: pass
            except: 
                pass
        else:
             item.setData(Qt.ItemDataRole.UserRole, None)
             
        self.setItem(row, 3, item)
        
        # Duration
        dur = self.get_duration(video_path) if video_path else 0.0
        self.setItem(row, 4, QTableWidgetItem(str(dur) + "s"))
        self.item(row, 4).setData(Qt.ItemDataRole.UserRole, dur)

    def get_duration(self, path):
        try:
            cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, **get_subprocess_kwargs())
            return float(res.stdout.strip())
        except: return 0.0

    def notify_change(self):
        # Try to call parent save method if possible.
        # Since we don't have direct ref to parent method easily, we can use signal or assume parent is VideoToolsApp
        # But `parent()` is technically the generic parent. 
        # Easier: The user (VideoToolsApp) should explicitly call save when this widget modification happens?
        # Actually, let's just make VideoToolsApp handle the saving logic by connecting signals later, 
        # OR we just re-implement save_finishing_state in main logic to be called often.
        # For simplicity, I will adapt VideoToolsApp to listen to manual changes or just leave it for Batch button.
        # The prompt asks for persistency, so Drag-Swap should trigger save.
        pass

# --- MAIN APP ---
def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

def get_subprocess_kwargs():
    """ Returns platform-specific kwargs to hide console window on Windows """
    kwargs = {}
    if os.name == 'nt':
        kwargs['creationflags'] = 0x08000000 # CREATE_NO_WINDOW
    return kwargs

def open_file_native(path):
    """ Opens a file or directory using the OS native handler """
    if not os.path.exists(path): return
    
    if os.name == 'nt':
        try:
            os.startfile(path)
        except Exception as e:
            print(f"Error opening file (Win): {e}")
    else:
        # macOS / Linux
        try:
            if sys.platform == "darwin":
                subprocess.run(["open", path])
            else:
                subprocess.run(["xdg-open", path])
        except Exception as e:
            print(f"Error opening file (Unix): {e}")

class VideoToolsApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Video Tools Suite")
        self.setWindowIcon(QIcon(resource_path("app_icon.png")))
        self.setGeometry(100, 100, 1200, 900)
        self.setStyleSheet("""
            QMainWindow, QDialog { background-color: #2b2b2b; color: #e0e0e0; }
            QWidget { font-family: "Segoe UI", "Arial", sans-serif; font-size: 14px; }
            
            /* Labels */
            QLabel { color: #e0e0e0; }
            
            /* Buttons */
            QPushButton {
                background-color: #3a3a3a;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 6px 12px;
                color: white;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #4a4a4a; border-color: #666; }
            QPushButton:pressed { background-color: #2a2a2a; border-color: #444; }
            QPushButton:disabled { background-color: #2b2b2b; color: #666; border-color: #333; }

            /* Primary Action Button (Blue) */
            QPushButton[text*="Refresh"], QPushButton[text*="Upload"], QPushButton[text*="Render"] {
                background-color: #007AFF; border: 1px solid #005BB5;
            }
            QPushButton[text*="Refresh"]:hover, QPushButton[text*="Upload"]:hover, QPushButton[text*="Render"]:hover {
                background-color: #1a8bff;
            }

            /* Inputs */
            QLineEdit, QTextEdit, QSpinBox, QComboBox {
                background-color: #1e1e1e;
                border: 1px solid #444;
                border-radius: 4px;
                padding: 4px;
                color: white;
                selection-background-color: #007AFF;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background-color: #1e1e1e;
                color: white;
                selection-background-color: #007AFF;
            }

            /* Table */
            QTableWidget {
                background-color: #1e1e1e;
                gridline-color: #333;
                border: 1px solid #444;
                color: #f0f0f0;
                alternate-background-color: #252525;
            }
            QTableWidget::item:selected { background-color: #007AFF; color: white; }
            QHeaderView::section {
                background-color: #333;
                color: white;
                padding: 6px;
                border: 1px solid #222;
                font-weight: bold;
            }

            /* Tabs */
            QTabWidget::pane { border: 1px solid #444; }
            QTabBar::tab {
                background-color: #2b2b2b;
                color: #aaa;
                padding: 8px 16px;
                border: 1px solid #444;
                border-bottom: none;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background-color: #3a3a3a;
                color: white;
                border-bottom: 2px solid #007AFF;
            }
            QTabBar::tab:hover { background-color: #333; color: white; }

            /* Group Box */
            QGroupBox {
                border: 1px solid #444;
                border-radius: 6px;
                margin-top: 12px;
                padding-top: 10px;
                font-weight: bold;
                color: #ccc;
            }
            QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 0 5px; }

            /* Message Box */
            QMessageBox { background-color: #2b2b2b; }
            QMessageBox QLabel { color: #e0e0e0; }
            QMessageBox QPushButton { min-width: 80px; }
            
            /* Scroll Bar */
            QScrollBar:vertical {
                border: none; background: #2b2b2b; width: 12px; margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #555; min-height: 20px; border-radius: 6px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
        """)

        # Check FFmpeg
        self.check_ffmpeg()
        
        # Data
        self.project_data = {} 
        self.current_video_path = ""
        self.video_duration = 0.0
        self.image_widgets = []
        self.clip_table = None
        self.slides_dir = None
        self.generated_videos = []

        central = QWidget()
        self.setCentralWidget(central)
        self.main_layout = QVBoxLayout(central)
        self.main_layout.setSpacing(10)

        self.setup_header() # Keep this line as it was before the new insertion.



        self.tabs = QTabWidget()
        self.setup_prepare_tab() 
        self.setup_extract_tab()
 
        self.setup_finishing_tab() # New Tab
        self.main_layout.addWidget(self.tabs)

        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: gray;")
        self.main_layout.addWidget(self.status_label)
        
        self.progress = QProgressBar()
        self.progress.hide()
        self.main_layout.addWidget(self.progress)
        


    # Removed save_state


    def closeEvent(self, event):
        # Persistence removed
        super().closeEvent(event)

    def reset_project(self):
        reply = QMessageBox.question(self, "Confirm Reset", 
                                     "Are you sure you want to Refresh? This will clear all current work.",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.No: return

        # 1. Clear Data
        self.current_video_path = ""
        self.video_duration = 0.0
        self.slides_dir = None
        
        # 2. Reset Header
        self.thumb_label.setText("")
        self.thumb_label.setPixmap(QPixmap()) 
        self.file_label.setText("No video selected")
        
        # 3. Reset Inputs (Prepare)
        if hasattr(self, 'logo_path_input'): self.logo_path_input.setText("")
        if hasattr(self, 'wm_x'): self.wm_x.setText("1060")
        if hasattr(self, 'wm_y'): self.wm_y.setText("640")
        if hasattr(self, 'chk_trim'): self.chk_trim.setChecked(True)
        if hasattr(self, 'trim_seconds_input'): self.trim_seconds_input.setText("3")
        self.btn_process.setEnabled(False)
        
        # 4. Reset Extract
        if hasattr(self, 'results_grid'):
            while self.results_grid.count():
                item = self.results_grid.takeAt(0)
                w = item.widget()
                if w: w.deleteLater()
        self.image_widgets = []
        
        # 5. Reset Finishing
        if self.clip_table:
            self.clip_table.setRowCount(0)
        if hasattr(self, 'btn_assemble'): self.btn_assemble.setEnabled(True)
        
        # 6. Go to Tab 1
        self.tabs.setCurrentIndex(0)
        self.status_label.setText("Project Reset. Ready.")

    def setup_header(self):
        header_frame = QFrame()
        layout = QHBoxLayout(header_frame)
        self.thumb_label = QLabel()
        self.thumb_label.setFixedSize(80, 50)
        self.thumb_label.setStyleSheet("border: 1px solid #555; background-color: black;")
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.thumb_label)
        
        # Refresh Button (Replaces Home)
        btn_refresh = QPushButton("🔄 Refresh")
        btn_refresh.setFixedWidth(100)
        btn_refresh.setStyleSheet("background-color: #FF9800; color: white; padding: 5px; font-weight: bold;")
        btn_refresh.clicked.connect(self.reset_project)
        layout.addWidget(btn_refresh)

        self.file_label = QLabel("No video selected")
        self.file_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #ccc;")
        layout.addWidget(self.file_label)
        btn = QPushButton("Select Video")
        btn.setFixedWidth(120)
        btn.clicked.connect(self.select_video)
        layout.addWidget(btn)
        self.main_layout.addWidget(header_frame)

    def setup_prepare_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        lbl = QLabel("1. Prepare Video")
        lbl.setFont(QFont("Arial", 16, QFont.Weight.Bold))
        layout.addWidget(lbl)
        gb_wm = QGroupBox("Add Watermark (Logo)")
        v_wm = QVBoxLayout(gb_wm)
        hbox = QHBoxLayout()
        self.logo_path_input = QLineEdit()
        self.logo_path_input.setPlaceholderText("Path to logo.png")
        btn_logo = QPushButton("...")
        btn_logo.setFixedWidth(40)
        btn_logo.clicked.connect(self.select_logo)
        hbox.addWidget(QLabel("File:"))
        hbox.addWidget(self.logo_path_input)
        hbox.addWidget(btn_logo)
        v_wm.addLayout(hbox)
        form_wm = QFormLayout()
        self.wm_x = QLineEdit("1060")
        self.wm_y = QLineEdit("640")
        form_wm.addRow("Pos X:", self.wm_x)
        form_wm.addRow("Pos Y:", self.wm_y)
        v_wm.addLayout(form_wm)
        
        # Logo Preview
        self.logo_preview = QLabel("No Logo")
        self.logo_preview.setFixedSize(100, 60)
        self.logo_preview.setStyleSheet("border: 1px solid #555; background: #222;")
        self.logo_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hbox.addWidget(self.logo_preview) # Add to hbox
        
        layout.addWidget(gb_wm)
        gb_trim = QGroupBox("Trim Video")
        v_trim = QVBoxLayout(gb_trim)
        self.chk_trim = QCheckBox("Trim from END")
        self.chk_trim.setChecked(True)
        v_trim.addWidget(self.chk_trim)
        form_trim = QFormLayout()
        self.trim_seconds_input = QLineEdit("3")
        form_trim.addRow("Seconds to cut:", self.trim_seconds_input)
        v_trim.addLayout(form_trim)
        layout.addWidget(gb_trim)
        self.btn_process = QPushButton("Process Video (Apply Logo & Trim)")
        self.btn_process.setFixedHeight(40)
        self.btn_process.clicked.connect(self.run_process)
        self.btn_process.setEnabled(False)
        layout.addWidget(self.btn_process)
        self.tabs.addTab(tab, "1. Prepare")

    def setup_extract_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        lbl = QLabel("2. Extract Slides")
        lbl.setFont(QFont("Arial", 16, QFont.Weight.Bold))
        layout.addWidget(lbl)
        toolbar = QHBoxLayout()
        self.btn_extract = QPushButton("Extract Slides")
        self.btn_extract.clicked.connect(self.run_extract)
        self.btn_extract.setEnabled(False)
        toolbar.addWidget(self.btn_extract)
        toolbar.addStretch()
        btn_sel_all = QPushButton("Select All")
        btn_sel_all.clicked.connect(lambda: self.set_all_selected(True))
        toolbar.addWidget(btn_sel_all)
        btn_desel_all = QPushButton("Deselect All")
        btn_desel_all.clicked.connect(lambda: self.set_all_selected(False))
        toolbar.addWidget(btn_desel_all)
        # Removed Vertex Button
        layout.addLayout(toolbar)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.results_container = QWidget()
        self.results_grid = QGridLayout(self.results_container)
        self.scroll_area.setWidget(self.results_container)
        layout.addWidget(self.scroll_area)
        self.tabs.addTab(tab, "2. Extract")

    # Removed setup_vertex_tab

    def setup_finishing_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        lbl = QLabel("3. Final Assembly")
        lbl.setFont(QFont("Arial", 16, QFont.Weight.Bold))
        layout.addWidget(lbl)
        
        # Tools Layout
        hbox_tools = QHBoxLayout()
        btn_refresh = QPushButton("🔄 Refresh Frame List")
        btn_refresh.clicked.connect(self.populate_clips_table)
        hbox_tools.addWidget(btn_refresh)
        
        btn_batch = QPushButton("📁 Upload Videos (Batch)")
        btn_batch.setStyleSheet("background-color: #2196F3; font-weight: bold;")
        btn_batch.clicked.connect(self.batch_upload_videos)

        hbox_tools.addWidget(btn_batch)
        layout.addLayout(hbox_tools)
        
        self.clip_table = ClipTableWidget()
        layout.addWidget(self.clip_table)
        
        # Render Options
        gb_render = QGroupBox("Render Options")
        hbox_render = QHBoxLayout(gb_render)
        
        self.chk_mix_audio = QCheckBox("Mix Original Audio")
        self.chk_mix_audio.setChecked(True)
        hbox_render.addWidget(self.chk_mix_audio)
        
        hbox_render.addWidget(QLabel("Generated Audio Vol:"))
        self.slider_vol = QSlider(Qt.Orientation.Horizontal)
        self.slider_vol.setRange(0, 100)
        self.slider_vol.setValue(10)
        self.slider_vol.setFixedWidth(150)
        hbox_render.addWidget(self.slider_vol)
        
        self.lbl_vol_val = QLabel("10%")
        self.slider_vol.valueChanged.connect(lambda v: self.lbl_vol_val.setText(f"{v}%"))
        hbox_render.addWidget(self.lbl_vol_val)
        
        # Spacer
        hbox_render.addSpacing(20)
        
        # New Zoom Options
        self.chk_auto_zoom = QCheckBox("Auto-Zoom Missing Videos")
        self.chk_auto_zoom.setChecked(True)
        hbox_render.addWidget(self.chk_auto_zoom)
        
        hbox_render.addWidget(QLabel("Zoom %:"))
        self.spin_zoom = QSpinBox()
        self.spin_zoom.setRange(100, 200)
        self.spin_zoom.setValue(110)
        self.spin_zoom.setSuffix("%")
        hbox_render.addWidget(self.spin_zoom)
        
        # Link Checkbox to Spinbox
        self.chk_auto_zoom.toggled.connect(self.spin_zoom.setEnabled)
        
        # Audio Normalization
        self.chk_norm = QCheckBox("Normalize Audio (Loudness)")
        hbox_render.addWidget(self.chk_norm)
        
        # Transitions
        hbox_render.addWidget(QLabel("Transition:"))
        self.combo_trans = QComboBox()
        self.combo_trans.addItems(["None", "Fade Black", "Fade White"])
        hbox_render.addWidget(self.combo_trans)

        layout.addWidget(gb_render)
        
        hbox_action = QHBoxLayout()
        self.btn_assemble = QPushButton("🎬 Render Final Video")
        self.btn_assemble.setStyleSheet("background-color: #E91E63; font-size: 14px; font-weight: bold; padding: 10px;")
        self.btn_assemble.clicked.connect(self.run_assembly)
        hbox_action.addStretch()
        hbox_action.addWidget(self.btn_assemble)
        layout.addLayout(hbox_action)
        
        self.tabs.addTab(tab, "3. Finishing")
    
    def batch_upload_videos(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Select Videos in Batch", "", "Video Files (*.mp4 *.mov *.avi)")
        if not paths: return
        
        import re
        # Relaxed Regex: Find ANY leading number or number sequence
        # Old: ^\s*(\d+)
        # New: Search for (\d+) and use the first match.
        
        parsed_files = []
        for p in paths:
            fname = os.path.basename(p)
            match = re.search(r"(\d+)", fname)
            if match:
                num = int(match.group(1))
                parsed_files.append((num, p))
            else:
                print(f"DEBUG: Skipping batch file (no number found): {fname}")
        
        # Sort by number
        parsed_files.sort(key=lambda x: x[0])
        
        if not parsed_files:
            QMessageBox.warning(self, "No Match", "No numbers found in filenames to sort by.")
            return
            
        # Assign to rows
        count = 0
        for num, deep_path in parsed_files:
            # num indicates the 1-based index (usually) or raw number.
            # User wants: frame 1 -> video 01. So index = num - 1.
            row_idx = num - 1 
            if 0 <= row_idx < self.clip_table.rowCount():
                self.clip_table.set_video_for_row(row_idx, deep_path)
                count += 1
        
        QMessageBox.information(self, "Batch Complete", f"Assigned {count} videos based on detected numbers.")
        self.save_finishing_state()

    def save_finishing_state(self):
        pass # Persistence disabled

    def populate_clips_table(self):
        # 1. Get Frames from Extract Folder
        slides_dir = self.slides_dir
        if not slides_dir and self.current_video_path:
             # Ensure absolute normalized path for Windows
             slides_dir = os.path.dirname(os.path.abspath(self.current_video_path))

        if not slides_dir or not os.path.exists(slides_dir):
            QMessageBox.warning(self, "No Slides", f"Slides directory invalid or not found:\n{slides_dir}")
            return
        
        # Normalize and list
        slides_dir = os.path.normpath(slides_dir)
        try:
            files = sorted([f for f in os.listdir(slides_dir) if f.startswith("frame_") and f.endswith(".png")])
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to list frames:\n{e}")
            return
            
        if not files:
             QMessageBox.warning(self, "No Frames", f"No timestamped frames found in folder:\n{slides_dir}")
             return

        self.clip_table.setRowCount(len(files))
        
        # 2. Parse Timestamps
        t_stamps = []
        for f in files:
            # frame_0001__01-23-456.png
            try:
                part = f.split("__")[1].split(".")[0] # 01-23-456
                mins, secs, mills = map(int, part.split("-"))
                total_sec = (mins * 60) + secs + (mills / 1000.0)
                t_stamps.append(total_sec)
            except:
                t_stamps.append(0.0)

        # Load Saved Mapping for restoration
        saved_map = {}

        # 3. Fill Table
        for i, f_name in enumerate(files):
            # A. Thumbnail
            path = os.path.join(slides_dir, f_name)
            item_thumb = QTableWidgetItem()
            pix = QPixmap(path)
            if not pix.isNull():
                 item_thumb.setIcon(QIcon(pix))
            # STORE FULL PATH for fallback logic
            item_thumb.setData(Qt.ItemDataRole.UserRole, path)
            self.clip_table.setItem(i, 0, item_thumb)
            
            # B. Timestamp
            ts = t_stamps[i]
            mins = int(ts // 60)
            secs = int(ts % 60)
            millis = int((ts * 1000) % 1000)
            self.clip_table.setItem(i, 1, QTableWidgetItem(f"{mins:02d}:{secs:02d}.{millis:03d}"))
            
            # C. Target Duration
            if i < len(t_stamps) - 1:
                dur = t_stamps[i+1] - t_stamps[i]
            else:
                # Last slide logic: Default to 8s or derive from video duration if possible
                dur = 8.0 
                # Try to get total video duration
                if self.video_duration > 0:
                     rem = self.video_duration - t_stamps[i]
                     if rem > 0: dur = rem
            
            # Sanity check
            if dur < 0.1: dur = 2.0 # Fallback
            
            self.clip_table.setItem(i, 2, QTableWidgetItem(f"{dur:.2f}s"))
            self.clip_table.item(i, 2).setData(Qt.ItemDataRole.UserRole, dur) # Store float
            
            # D. Init Video Slots (Restore or Empty)
            saved_vid = saved_map.get(str(i))
            if saved_vid and os.path.exists(saved_vid):
                 self.clip_table.set_video_for_row(i, saved_vid)
            else:
                self.clip_table.setItem(i, 3, QTableWidgetItem("Drop Video Here"))
                self.clip_table.item(i, 3).setData(Qt.ItemDataRole.UserRole, None)
                self.clip_table.setItem(i, 4, QTableWidgetItem("-"))
            
            # E. Action
            btn_clear = QPushButton("X")
            btn_clear.clicked.connect(lambda _, r=i: self.clear_row_video(r))
            self.clip_table.setCellWidget(i, 5, btn_clear)

        self.clip_table.resizeRowsToContents()

    def clear_row_video(self, row):
        self.clip_table.setItem(row, 3, QTableWidgetItem("Drop Video Here"))
        self.clip_table.item(row, 3).setData(Qt.ItemDataRole.UserRole, None)
        self.clip_table.setItem(row, 4, QTableWidgetItem("-"))
        self.save_finishing_state()

    def run_assembly(self):
        clip_data = []
        rows = self.clip_table.rowCount()
        if rows == 0: return

        for i in range(rows):
            # Video
            item_vid = self.clip_table.item(i, 3)
            video_path = item_vid.data(Qt.ItemDataRole.UserRole)
            
            # Frame Image (Fallback)
            item_thumb = self.clip_table.item(i, 0)
            frame_path = item_thumb.data(Qt.ItemDataRole.UserRole)
            
            # Check Fallback
            use_zoom = self.chk_auto_zoom.isChecked()
            
            if not video_path:
                if use_zoom and frame_path and os.path.exists(frame_path):
                    # Will use Zoom Fallback
                    pass 
                else:
                    QMessageBox.warning(self, "Missing Video", f"Row {i+1} has no video assigned and Auto-Zoom is unavailable.")
                    return
            
            # Target Dur
            item_target = self.clip_table.item(i, 2)
            target = item_target.data(Qt.ItemDataRole.UserRole)
            
            # Source Dur
            item_source = self.clip_table.item(i, 4)
            source = item_source.data(Qt.ItemDataRole.UserRole)
            if not source: source = target 
            
            clip_data.append({
                "video": video_path,
                "image": frame_path,
                "target_dur": float(target),
                "source_dur": float(source)
            })

        # Output Path
        base_dir = self.slides_dir or os.path.dirname(self.current_video_path)
        output = os.path.join(os.path.dirname(base_dir), "final_assembly.mp4")
        
        # Mix Settings
        mix_settings = {
            "enabled": self.chk_mix_audio.isChecked(),
            "original_path": self.current_video_path,
            "generated_vol": self.slider_vol.value() / 100.0,
            "zoom_amount": self.spin_zoom.value(),
            "audio_norm": self.chk_norm.isChecked(),
            "transition": self.combo_trans.currentText()
        }
        if mix_settings["enabled"] and not self.current_video_path:
             # Just a safety check
             mix_settings["enabled"] = False
        
        self.assembly_worker = AssemblyWorker(clip_data, output, mix_settings)
        self.assembly_worker.progress_signal.connect(lambda a, b, msg: self.status_label.setText(msg)) # Simple status update
        self.assembly_worker.finished_signal.connect(self.on_assembly_done)
        
        self.btn_assemble.setEnabled(False)
        self.assembly_worker.start()

    def on_assembly_done(self, success, result):
        self.btn_assemble.setEnabled(True)
        self.status_label.setText("Ready")
        if success:
            QMessageBox.information(self, "Success", f"Video assembled!\nSaved to: {result}")
            subprocess.run(["open", "-R", result])
        else:
            QMessageBox.critical(self, "Error", f"Assembly failed:\n{result}")

    def select_video(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Video", "", "MP4 Files (*.mp4)")
        if path:
            self.set_current_video(path)
            self.get_video_duration(path)
    
    def select_logo(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Logo", "", "Images (*.png *.jpg)")
        if path:
            self.logo_path_input.setText(path)
            pix = QPixmap(path)
            if not pix.isNull():
                 self.logo_preview.setPixmap(pix.scaled(100, 60, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            else:
                 self.logo_preview.setText("Invalid Img")

    def select_key_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Key", "", "JSON (*.json)")
        if path:
            self.vertex_key_path = path
            self.lbl_key_status.setText("✅ Custom Key Selected")
            self.lbl_key_status.setStyleSheet("color: #FFC107; font-weight: bold;")

    def set_current_video(self, path):
        self.current_video_path = path
        self.file_label.setText(os.path.basename(path))
        self.file_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #4CAF50;") 
        self.generate_thumbnail(path)
        self.btn_process.setEnabled(True)
        self.btn_extract.setEnabled(True)
        # self.save_state()

    def check_ffmpeg(self):
        # 1. Try bundled FFmpeg (Windows)
        # Look for 'ffmpeg' folder in resource path
        bundled_ffmpeg = resource_path(os.path.join("ffmpeg", "bin"))
        if os.path.exists(bundled_ffmpeg):
            os.environ["PATH"] += os.pathsep + bundled_ffmpeg
            print(f"Added bundled FFmpeg to PATH: {bundled_ffmpeg}")

        # 2. Verify availability
        try:
            subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **get_subprocess_kwargs())
        except FileNotFoundError:
            QMessageBox.critical(self, "FFmpeg Missing", 
                "FFmpeg is not found.\n\n"
                "Please install FFmpeg or ensure the 'ffmpeg' folder is in the app directory."
            )

    def generate_thumbnail(self, video_path):
        temp_thumb = os.path.join(os.path.dirname(video_path), ".temp_thumb.jpg")
        try:
            cmd = ["ffmpeg", "-ss", "00:00:01", "-i", video_path, "-vframes", "1", "-vf", "scale=160:-1", temp_thumb, "-y"]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **get_subprocess_kwargs())
            if os.path.exists(temp_thumb):
                pix = QPixmap(temp_thumb)
                self.thumb_label.setPixmap(pix.scaled(80, 50, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation))
                try: os.remove(temp_thumb)
                except: pass
            else:
                self.thumb_label.setText("No Img")
        except FileNotFoundError:
            self.thumb_label.setText("No FFmpeg")
        except Exception as e:
            print(f"Thumb error: {e}")

    def get_video_duration(self, path):
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, **get_subprocess_kwargs())
            self.video_duration = float(res.stdout.strip())
        except FileNotFoundError:
            self.video_duration = 0.0
            QMessageBox.warning(self, "FFmpeg Error", "Could not get video duration (FFmpeg/FFprobe missing).")
        except Exception: 
            self.video_duration = 0.0

    def run_process(self):
        if not self.current_video_path: return
        base, ext = os.path.splitext(self.current_video_path)
        output_path = f"{base}_processed{ext}"
        cmd = ["ffmpeg", "-i", self.current_video_path]
        logo = self.logo_path_input.text()
        has_logo = os.path.exists(logo)
        if has_logo:
             cmd.extend(["-i", logo])
        if has_logo:
             cmd.extend(["-filter_complex", f"overlay={self.wm_x.text()}:{self.wm_y.text()}"])
        if self.chk_trim.isChecked():
            try:
                # Safety Check: Ensure we have duration
                if self.video_duration <= 0:
                     self.get_video_duration(self.current_video_path)
                     
                cut_sec = float(self.trim_seconds_input.text())
                new_dur = max(1.0, self.video_duration - cut_sec) # Ensure at least 1s
                cmd.extend(["-t", str(new_dur)])
            except: 
                QMessageBox.warning(self, "Trim Error", "Invalid trim duration or video length unknown.")
                return
        cmd.extend(["-c:v", "libx264", "-c:a", "copy", output_path, "-y"])
        self.start_ffmpeg_worker(cmd, 'process', output_path)

    def run_extract(self):
        if not self.current_video_path: return
        video_dir = os.path.dirname(self.current_video_path)
        output_pattern = os.path.join(video_dir, "slide_%04d.png")
        
        # FIX: Removed :file=/dev/stderr (incompatible with Windows). 
        # metadata=print automatically prints to stderr, which we capture.
        cmd = ["ffmpeg", "-i", self.current_video_path, "-vf", "select='eq(n,0)+gt(scene,0.12)',metadata=print", "-vsync", "vfr", output_pattern, "-y"]
        self.start_ffmpeg_worker(cmd, 'extract', video_dir)

    def start_ffmpeg_worker(self, cmd, task_type, expected_output):
        self.progress.setRange(0, 0)
        self.progress.show()
        self.current_task_output = expected_output 
        self.worker = FFmpegWorker(cmd, task_type)
        self.worker.finished.connect(self.on_ffmpeg_done)
        self.worker.start()

    def on_ffmpeg_done(self, success, msg, output_log):
        self.progress.hide()
        if success:
            if self.worker.task_type == 'process':
                msg_box = QMessageBox(self)
                msg_box.setWindowTitle("Video Ready")
                msg_box.setText("Video has been processed successfully!")
                btn_open_video = msg_box.addButton("Open Video", QMessageBox.ButtonRole.ActionRole)
                btn_open_folder = msg_box.addButton("Open Folder", QMessageBox.ButtonRole.ActionRole)
                msg_box.addButton("OK", QMessageBox.ButtonRole.AcceptRole)
                msg_box.exec()
                if msg_box.clickedButton() == btn_open_video:
                    if os.path.isfile(self.current_task_output): open_file_native(self.current_task_output)
                elif msg_box.clickedButton() == btn_open_folder:
                     open_file_native(os.path.dirname(self.current_task_output))
                if os.path.isfile(self.current_task_output):
                    self.set_current_video(self.current_task_output)
                    self.get_video_duration(self.current_task_output)
                    self.tabs.setCurrentIndex(1)
            elif self.worker.task_type == 'extract':
                if os.path.isdir(self.current_task_output):
                    # --- TIMESTAMP PROCESSING START ---
                    try:
                        import re
                        # 1. Parse timestamps from stderr log
                        # Format in log: "pts_time:12.345678"
                        timestamps = re.findall(r'pts_time:([0-9\.]+)', output_log)
                        
                        # 2. Get generated files (sorted)
                        directory = self.current_task_output
                        files = sorted([f for f in os.listdir(directory) if f.startswith("slide_") and f.endswith(".png")])
                        
                        # 3. Rename loop
                        renamed_count = 0
                        for i, filename in enumerate(files):
                            if i < len(timestamps):
                                ts_float = float(timestamps[i])
                                # Calculate MM-SS-mmm
                                minutes = int(ts_float // 60)
                                seconds = int(ts_float % 60)
                                millis = int((ts_float * 1000) % 1000)
                                ts_str = f"{minutes:02d}-{seconds:02d}-{millis:03d}"
                                
                                new_name = f"frame_{i+1:04d}__{ts_str}.png"
                                old_path = os.path.join(directory, filename)
                                new_path = os.path.join(directory, new_name)
                                os.rename(old_path, new_path)
                                renamed_count += 1
                        
                        print(f"DEBUG: Renamed {renamed_count} files with timestamps.")
                        
                    except Exception as e:
                        print(f"Error processing timestamps: {e}")
                    # --- TIMESTAMP PROCESSING END ---

                    self.load_gallery(self.current_task_output)
                    QMessageBox.information(self, "Success", "Slides extracted with timestamps!")
                    open_file_native(self.current_task_output)
        else:
            QMessageBox.critical(self, "Error", msg)

    def load_gallery(self, directory):
        self.slides_dir = directory
        # self.save_state()
        
        for i in reversed(range(self.results_grid.count())): 
            self.results_grid.itemAt(i).widget().setParent(None)
           # Support loading from folder even if images aren't named "slide_" if imported manually
        self.image_widgets = []
        images = sorted([f for f in os.listdir(directory) if f.lower().endswith(('.png', '.jpg', '.jpeg'))])
        if not images: return
        row, col = 0, 0
        self.current_gallery_images = [os.path.join(directory, img) for img in images] 
        for idx, img_file in enumerate(images):
            abs_path = os.path.join(directory, img_file)
            widget = SelectableImageWidget(abs_path)
            
            # --- PARSE TIMESTAMP FOR UI ---
            # Expected format: frame_0001__01-23-456.png
            if "__" in img_file:
                try:
                    # extract 01-23-456.png
                    parts = img_file.split("__")
                    if len(parts) > 1:
                        ts_part = parts[1].split(".")[0] # 01-23-456
                        # Convert to 01:23.456
                        formatted = ts_part.replace("-", ":", 1).replace("-", ".", 1)
                        widget.set_timestamp(formatted)
                except: pass
            # ------------------------------

            widget.thumb_label.clicked.connect(lambda i=idx: self.open_lightbox(i))
            self.results_grid.addWidget(widget, row, col)
            self.image_widgets.append(widget)
            col += 1
            if col >= 4:
                col = 0
                row += 1

    def open_lightbox(self, index):
        if not self.current_gallery_images: return
        dlg = LightboxDialog(self.current_gallery_images, index, self)
        dlg.exec()

    def set_all_selected(self, selected):
        for w in self.image_widgets: w.checkbox.setChecked(selected)

    def send_to_ai_tab(self):
        selected_paths = [w.image_path for w in self.image_widgets if w.checkbox.isChecked()]
        if not selected_paths:
            QMessageBox.warning(self, "No Images", "Please select at least one image.")
            return
        self.target_images = selected_paths
        self.lbl_batch_info.setText(f"Ready to process: {len(selected_paths)} images selected")
        self.lbl_batch_info.setStyleSheet("color: #00E676; font-weight: bold; font-size: 14px;")
        self.tabs.setCurrentIndex(2)

    def run_vertex_generation(self):
        if not self.vertex_key_path or not os.path.exists(self.vertex_key_path):
            QMessageBox.warning(self, "Error", f"Service Account Key not found.\nExpected: {self.vertex_key_path}")
            return
            
        if not self.target_images:
            QMessageBox.warning(self, "Error", "No images selected from gallery.\nPlease go to 'Extract Slides' and click 'Send to Vertex AI'.")
            return

        self.btn_generate.setEnabled(False)
        self.progress.setRange(0, len(self.target_images))
        self.progress.show()

        model = self.combo_model.currentText()
        prompt = self.prompt_text.toPlainText()
        duration = self.combo_dur.currentText()

        self.v_worker = VertexWorker(self.vertex_key_path, self.target_images, model, prompt, duration)
        self.v_worker.progress_signal.connect(self.on_vertex_progress)
        self.v_worker.video_generated.connect(self.add_video_result)
        self.v_worker.finished_signal.connect(self.on_vertex_finished)
        self.v_worker.start()

    def on_vertex_progress(self, current, total, msg):
        self.progress.setValue(current)
        self.status_label.setText(f"{msg} ({current}/{total})")

    def add_video_result(self, video_path):
        wid = QWidget()
        hbox = QHBoxLayout(wid)
        hbox.setContentsMargins(0, 0, 0, 0)
        lbl_name = QLabel(os.path.basename(video_path))
        hbox.addWidget(lbl_name)
        btn_play = QPushButton("Play")
        btn_play.clicked.connect(lambda: subprocess.run(["open", video_path]))
        hbox.addWidget(btn_play)
        btn_show = QPushButton("Show in Finder")
        btn_show.clicked.connect(lambda: subprocess.run(["open", "-R", video_path]))
        hbox.addWidget(btn_show)
        self.v_results_layout.addWidget(wid)

    def on_vertex_finished(self, success, msg):
        self.btn_generate.setEnabled(True)
        self.progress.hide()
        if success:
            QMessageBox.information(self, "Batch Complete", "All videos processed!")
        else:
            QMessageBox.critical(self, "API Error", msg)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # --- APP IDENTITY ---
    app.setApplicationName("Video Tools Suite")
    app.setApplicationDisplayName("Video Tools Suite")
    
    # Load Icon
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_icon.png")
    if os.path.exists(icon_path):
        app_icon = QIcon(icon_path)
        app.setWindowIcon(app_icon)
        # MacOS specific: This often helps set the Dock icon for non-packaged apps
        try:
            app.setWindowIcon(app_icon) 
        except: pass
    # --------------------

    # Launch VideoToolsApp Directly
    window = VideoToolsApp()
    window.show()
    sys.exit(app.exec())