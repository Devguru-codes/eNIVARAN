import os
import tempfile
import base64
import sqlite3
import io
import datetime
import json
from flask import Flask, request, render_template, jsonify, send_from_directory, session, redirect, url_for, flash
from flask.json.provider import DefaultJSONProvider
import logging
from flask.logging import default_handler
import firebase_admin
from firebase_admin import credentials, db
# --- NEW IMPORTS ---
from dotenv import load_dotenv
import google.generativeai as genai

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('eNivaran')
logger.addHandler(default_handler)

def handle_json_error(e):
    """Handle JSON serialization errors"""
    logger.error(f"JSON Serialization Error: {str(e)}")
    return jsonify({
        'error': 'Internal server error occurred while processing the request.',
        'details': str(e) if app.debug else None
    }), 500

def handle_value_error(e):
    """Handle data type conversion errors"""
    logger.error(f"Value Error: {str(e)}")
    return jsonify({
        'error': 'Invalid data format in request.',
        'details': str(e) if app.debug else None
    }), 400

def handle_key_error(e):
    """Handle missing key errors"""
    logger.error(f"Key Error: {str(e)}")
    return jsonify({
        'error': 'Required data missing from request.',
        'details': str(e) if app.debug else None
    }), 400

def handle_sqlite_error(e):
    """Handle database errors"""
    logger.error(f"Database Error: {str(e)}")
    return jsonify({
        'error': 'Database operation failed.',
        'details': str(e) if app.debug else None
    }), 500

class CustomJSONEncoder(DefaultJSONProvider):
    def __init__(self, app):
        super().__init__(app)
        self.options = {
            'ensure_ascii': False,
            'sort_keys': False,
            'compact': True
        }

    def default(self, obj):
        try:
            if isinstance(obj, datetime.datetime):
                return obj.isoformat()
            if isinstance(obj, sqlite3.Row):
                return dict(obj)
            if isinstance(obj, bytes):
                return base64.b64encode(obj).decode('utf-8')
            return super().default(obj)
        except Exception as e:
            print(f"JSON encoding error: {e}")
            return None
        
    def dumps(self, obj, **kwargs):
        def convert(o):
            if isinstance(o, datetime.datetime):
                return o.isoformat()
            elif isinstance(o, sqlite3.Row):
                return dict(o)
            elif isinstance(o, bytes):
                return base64.b64encode(o).decode('utf-8')
            elif isinstance(o, dict):
                return {k: convert(v) for k, v in o.items()}
            elif isinstance(o, (list, tuple)):
                return [convert(v) for v in o]
            return o

        try:
            return json.dumps(convert(obj), ensure_ascii=False, **kwargs)
        except Exception as e:
            print(f"JSON dumps error: {e}")
            return json.dumps(None)

    def loads(self, s, **kwargs):
        try:
            return json.loads(s, **kwargs)
        except Exception as e:
            print(f"JSON loads error: {e}")
            return None

from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderServiceError

# Import the pothole detection function from the existing file
from pothole_detection import run_pothole_detection
from duplication_detection_code import get_duplicate_detector

app = Flask(__name__)
app.config['SECRET_KEY'] = 'dev-secret-key-replace-later'

# Initialize Firebase Admin SDK
try:
    cred = credentials.Certificate('firebase-service-account.json')
    # MAKE SURE THIS URL IS CORRECT
    firebase_admin.initialize_app(cred, {
        'databaseURL': 'https://enivaran-1e89f-default-rtdb.firebaseio.com'
    })
    app.logger.info("Firebase Admin SDK initialized successfully.")
except Exception as e:
    app.logger.error(f"Failed to initialize Firebase Admin SDK: {e}")
    raise e

# Initialize the duplicate detector
detector = get_duplicate_detector(location_threshold=0.1)  # 100-meter threshold

# --- START: AI CHATBOT CONFIGURATION ---
# IMPORTANT: Store your API key in a .env file in the root directory
# Create a file named .env and add the following line:
# GEMINI_API_KEY="YOUR_API_KEY_HERE"
# You can get a key from Google AI Studio: https://makersuite.google.com/app/apikey
try:
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not found. Please create a .env file.")
    genai.configure(api_key=GEMINI_API_KEY)
    chat_model = genai.GenerativeModel('gemini-2.5-flash')
    app.logger.info("Google Gemini AI configured successfully.")
except Exception as e:
    app.logger.error(f"Failed to configure Google Gemini AI: {e}")
    chat_model = None
# --- END: AI CHATBOT CONFIGURATION ---

def load_existing_complaints_into_detector():
    """
    Loads all existing, non-duplicate complaints from the database into the
    in-memory duplicate detector on application startup.
    """
    app.logger.info("Loading existing complaints into duplicate detector...")
    with sqlite3.connect(APP_DB) as conn:
        conn.row_factory = dict_factory
        # Load only original (non-duplicate) reports for comparison
        complaints_to_load = conn.execute(
            'SELECT id, text, location_lat, location_lon, issue_type, image FROM complaints WHERE is_duplicate = 0'
        ).fetchall()

        for complaint in complaints_to_load:
            if not all(k in complaint for k in ['id', 'text', 'location_lat', 'location_lon', 'issue_type', 'image']):
                app.logger.warning(f"Skipping incomplete complaint record: {complaint.get('id')}")
                continue
            
            report_dict = {
                'id': complaint['id'],
                'text': complaint['text'],
                'location': (complaint['location_lat'], complaint['location_lon']),
                'issue_type': complaint['issue_type'],
                'image_bytes': complaint['image']
            }
            detector.add_report(report_dict)
    
    app.logger.info(f"Loaded {len(complaints_to_load)} complaints into the detector.")
    # Optionally build clusters after loading
    if len(complaints_to_load) > 1:
        detector.build_clusters()
        app.logger.info("Detector clusters have been built.")

# Add Jinja2 filter for base64 encoding
def b64encode_filter(data):
    """Jinja2 filter to base64 encode binary data."""
    if data is None:
        return None
    return base64.b64encode(data).decode('utf-8')

app.jinja_env.filters['b64encode'] = b64encode_filter

# Configure JSON provider
app.json = CustomJSONEncoder(app)

# --- Application Configuration ---
BASE_DIR = os.path.dirname(__file__)
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
# --- NEW: Create a dedicated folder for chat file uploads ---
CHAT_UPLOAD_FOLDER = os.path.join(UPLOAD_FOLDER, 'chat_files')
os.makedirs(CHAT_UPLOAD_FOLDER, exist_ok=True)

app.config.update(
    UPLOAD_FOLDER=UPLOAD_FOLDER,
    SECRET_KEY='dev-secret-key-replace-later',  # Move this to environment variable in production
    MAX_CONTENT_LENGTH=16 * 1024 * 1024  # Limit file size to 16MB
)

# Register error handlers
app.register_error_handler(json.JSONDecodeError, handle_json_error)
app.register_error_handler(ValueError, handle_value_error)
app.register_error_handler(KeyError, handle_key_error)
app.register_error_handler(sqlite3.Error, handle_sqlite_error)

# Add generic error handlers
@app.errorhandler(404)
def not_found_error(error):
    if request.is_json:
        return jsonify({'error': 'Resource not found'}), 404
    flash('The requested page was not found.', 'error')
    return render_template('index.html'), 404

@app.errorhandler(500)
def internal_error(error):
    app.logger.error(f'Server Error: {error}')
    if request.is_json:
        return jsonify({'error': 'An internal server error occurred'}), 500
    flash('An unexpected error has occurred.', 'error')
    return render_template('index.html'), 500

@app.errorhandler(Exception)
def unhandled_exception(e):
    app.logger.error(f'Unhandled Exception: {str(e)}')
    if request.is_json:
        return jsonify({
            'error': 'An unexpected error occurred',
            'details': str(e) if app.debug else None
        }), 500
    flash('An unexpected error has occurred.', 'error')
    return render_template('index.html'), 500

# Configure file logging
if not app.debug:
    import logging.handlers
    file_handler = logging.handlers.RotatingFileHandler(
        'enivaran.log',
        maxBytes=1024 * 1024,  # 1 MB
        backupCount=10
    )
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
    ))
    file_handler.setLevel(logging.INFO)
    app.logger.addHandler(file_handler)
    app.logger.setLevel(logging.INFO)
    app.logger.info('eNivaran startup')

# --- Database Configuration ---
APP_DB = os.path.join(BASE_DIR, 'enivaran.db')

def dict_factory(cursor, row):
    """
    Convert SQLite Row to dictionary with improved timestamp handling and error logging.
    Invalid timestamps are preserved as strings for debugging and logged as warnings.
    """
    d = {}
    for idx, col in enumerate(cursor.description):
        value = row[idx]
        column_name = col[0]
        
        # Handle timestamp fields
        if isinstance(value, str) and column_name in ['submitted_at', 'detected_at', 'created_at', 'last_updated']:
            try:
                # Try parsing with microseconds
                value = datetime.datetime.strptime(value, '%Y-%m-%d %H:%M:%S.%f')
            except ValueError:
                try:
                    # Try parsing without microseconds
                    value = datetime.datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    # Log the invalid timestamp and preserve original value
                    app.logger.warning(
                        f"Invalid timestamp format in column {column_name}: {value!r}. "
                        f"Row data: {dict(zip([col[0] for col in cursor.description], row))}"
                    )
                    # Keep the original string value for debugging
                    pass
        
        d[column_name] = value
    return d

def get_coordinates_from_address(street, city, state, zipcode):
    geolocator = Nominatim(user_agent="eNivaran-app")
    address = f"{street}, {city}, {state}, {zipcode}, India"
    try:
        location = geolocator.geocode(address)
        return (location.latitude, location.longitude) if location else (None, None)
    except GeocoderServiceError:
        return None, None

from functools import wraps

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/tools')
@login_required
def tools():
    return render_template('tools.html')

# --- Database Setup ---
APP_DB = os.path.join(os.path.dirname(__file__), 'enivaran.db')

def init_database():
    with sqlite3.connect(APP_DB) as conn:
        conn.execute('PRAGMA foreign_keys = ON')  # Enable foreign key support
        c = conn.cursor()
        
        # Create users table first (for foreign key relationships)
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                full_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            
        # Create complaints table with foreign key to users
        c.execute('''
            CREATE TABLE IF NOT EXISTS complaints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT,
                location_lat REAL,
                location_lon REAL,
                issue_type TEXT,
                image BLOB,
                image_filename TEXT,
                submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_duplicate INTEGER DEFAULT 0,
                original_report_id INTEGER,
                user_id INTEGER,
                status TEXT DEFAULT 'Submitted',
                upvotes INTEGER DEFAULT 0,
                remarks TEXT DEFAULT 'Complaint sent for supervision.',
                FOREIGN KEY (user_id) REFERENCES users (id),
                FOREIGN KEY (original_report_id) REFERENCES complaints (id)
            )''')
            
        # Create pothole detections table
        c.execute('''
            CREATE TABLE IF NOT EXISTS pothole_detections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                input_image BLOB,
                input_filename TEXT,
                detection_result TEXT,
                annotated_image BLOB,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_id INTEGER,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )''')
            
        # Create pothole stats table
        c.execute('''
            CREATE TABLE IF NOT EXISTS pothole_stats (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                total_potholes INTEGER DEFAULT 0,
                high_priority_count INTEGER DEFAULT 0,
                medium_priority_count INTEGER DEFAULT 0,
                low_priority_count INTEGER DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            
        # Initialize pothole stats if empty
        if c.execute('SELECT COUNT(*) FROM pothole_stats').fetchone()[0] == 0:
            c.execute('INSERT INTO pothole_stats (id) VALUES (1)')
            
        # Create upvotes table to track user votes
        c.execute('''
            CREATE TABLE IF NOT EXISTS upvotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                complaint_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
                FOREIGN KEY (complaint_id) REFERENCES complaints (id) ON DELETE CASCADE,
                UNIQUE (user_id, complaint_id)
            )''')

        # Create indexes for better performance
        c.execute('CREATE INDEX IF NOT EXISTS idx_complaints_user_id ON complaints(user_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_complaints_submitted_at ON complaints(submitted_at)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_upvotes_user_complaint ON upvotes(user_id, complaint_id)')
        
        conn.commit()

# --- Initialize Application ---
def init_app():
    """Initialize application with complete setup sequence"""
    app.logger.info("Starting application initialization...")
    
    # Step 1: Initialize database
    try:
        init_database()
        app.logger.info("Database initialized successfully")
    except Exception as e:
        app.logger.error(f"Database initialization failed: {e}")
        raise
    
    # Step 2: Create required directories
    try:
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        app.logger.info(f"Created upload folder: {app.config['UPLOAD_FOLDER']}")
    except Exception as e:
        app.logger.error(f"Failed to create upload folder: {e}")
        raise
    
    # Step 3: Enable foreign key support for all connections
    @app.before_request
    def enable_foreign_keys():
        if request.endpoint != 'static_files':  # Skip for static files
            conn = sqlite3.connect(APP_DB)
            conn.execute('PRAGMA foreign_keys = ON')
            conn.close()
    
    # Step 4: Load existing complaints into the duplicate detector
    try:
        load_existing_complaints_into_detector()
        app.logger.info("Loaded existing complaints into duplicate detector")
    except Exception as e:
        app.logger.error(f"Failed to load complaints into detector: {e}")
        # Don't raise here as this is non-critical
        
    app.logger.info("Application initialization completed")

# Initialize the application
init_app()


@app.route('/detect_pothole', methods=['POST'])
@login_required
def detect_pothole():
    if 'image' not in request.files:
        return jsonify({'error': 'No image uploaded'}), 400
    file = request.files['image']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    filename = secure_filename(file.filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(file_path)

    result_json, annotated_image_bytes = run_pothole_detection(file_path)
    os.remove(file_path)

    if result_json is None:
        return jsonify({'error': 'Detection failed'}), 500

    annotated_image_b64 = base64.b64encode(annotated_image_bytes).decode('utf-8')
    return jsonify({'result': result_json, 'annotated_image_b64': annotated_image_b64})

@app.route('/static/<path:filename>')
def static_files(filename):
    # Added to serve the illustration image
    return send_from_directory('static', filename)

# --- Authentication Routes ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        login_type = request.form.get('loginType', 'user')
        
        if login_type == 'admin':
            if username == 'admin001' and password == 'admin$001':
                session['user_id'] = 'admin'
                session['username'] = 'admin001'
                session['is_admin'] = True
                flash('Welcome back, Administrator!', 'success')
                return redirect(url_for('admin_dashboard'))
            else:
                flash('Invalid admin credentials.', 'error')
                return render_template('login.html')
        else:
            with sqlite3.connect(APP_DB) as conn:
                conn.row_factory = dict_factory
                user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
                
            if user and check_password_hash(user['password_hash'], password):
                session['user_id'] = user['id']
                session['username'] = user['username']
                session['is_admin'] = False
                flash(f'Welcome back, {user["full_name"]}!', 'success')
                return redirect(url_for('index'))
            else:
                flash('Invalid username or password.', 'error')
    
    return render_template('login.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if 'user_id' in session:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form['username']
        full_name = request.form['full_name']
        password = request.form['password']
        print(f"Attempting signup for username: {username}, full_name: {full_name}, password: {password}") # Debug print
        if not all([username, full_name, password]):
            flash('All fields are required.', 'error')
            print("Missing fields detected.") # Debug print
            return redirect(url_for('signup'))
        try:
            with sqlite3.connect(APP_DB) as conn:
                if conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone():
                    flash('Username already exists.', 'error')
                    print(f"Username {username} already exists.") # Debug print
                    return render_template('signup.html')
                password_hash = generate_password_hash(password)
                print(f"Password hash generated: {password_hash}") # Debug print
                conn.execute('INSERT INTO users (username, full_name, password_hash) VALUES (?, ?, ?)',
                             (username, full_name, password_hash))
                conn.commit()
                print("User inserted and committed to DB.") # Debug print
        except sqlite3.Error as e:
            flash(f"Database error: {e}", "error")
            print(f"Database error during signup: {e}") # Debug print
            return render_template('signup.html')
        print("Signup successful, redirecting to login.") # Debug print
        flash('Signup successful! Please log in.', 'success')
        return redirect(url_for('login'))
    print("GET request for signup page.") # Debug print
    return render_template('signup.html')

@app.route('/logout')
def logout():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    # Store username temporarily for the message
    username = session.get('username', 'User')
    is_admin = session.get('is_admin', False)
    
    # Clear all session data
    session.clear()
    
    # Flash appropriate goodbye message
    if is_admin:
        flash('Administrator logged out successfully.', 'success')
    else:
        flash(f'Goodbye, {username}! You have been logged out successfully.', 'success')
    
    # Redirect to login page
    return redirect(url_for('login'))

# --- Admin Routes ---
@app.route('/admin')
@login_required
def admin_dashboard():
    if not session.get('is_admin'):
        return redirect(url_for('index'))

    search_id = request.args.get('search_id', type=int)

    # Dynamically build the WHERE clause and parameters
    where_conditions = []
    params = []

    if search_id:
        where_conditions.append("c.id = ?")
        params.append(search_id)

    # Construct the final WHERE clause string
    where_clause = ""
    if where_conditions:
        where_clause = "WHERE " + " AND ".join(where_conditions)

    with sqlite3.connect(APP_DB) as conn:
        conn.row_factory = dict_factory

        query = f'''
            SELECT 
                c.id, c.text, 
                CAST(c.location_lat AS FLOAT) as location_lat,
                CAST(c.location_lon AS FLOAT) as location_lon,
                c.issue_type, c.image, c.submitted_at, c.status,
                c.upvotes, c.remarks, c.is_duplicate, c.original_report_id,
                u.username, u.full_name as reporter_name, c.user_id
            FROM complaints c
            LEFT JOIN users u ON c.user_id = u.id
            {where_clause}
            ORDER BY c.submitted_at DESC
        '''
        
        complaints_raw = conn.execute(query, params).fetchall()
        
        # Process and validate each complaint
        processed_complaints = []
        for complaint in complaints_raw:
            try:
                # Create a clean dictionary with basic complaint info
                comp_dict = {
                    'id': int(complaint['id']),
                    'text': str(complaint['text'] or ''),
                    'location_lat': float(complaint['location_lat'] or 0),
                    'location_lon': float(complaint['location_lon'] or 0),
                    'issue_type': str(complaint['issue_type'] or ''),
                    'status': str(complaint['status'] or 'Submitted'),
                    'upvotes': int(complaint['upvotes'] or 0),
                    'remarks': str(complaint['remarks'] or ''),
                    'username': str(complaint['username'] or ''),
                    'reporter_name': str(complaint['reporter_name'] or ''),
                    'is_duplicate': bool(complaint.get('is_duplicate')),
                    'original_report_id': int(complaint['original_report_id']) if complaint.get('original_report_id') else None,
                    'user_id': int(complaint['user_id'])
                }

                # Handle datetime
                if isinstance(complaint['submitted_at'], str):
                    try:
                        submitted_at = datetime.datetime.strptime(complaint['submitted_at'], '%Y-%m-%d %H:%M:%S.%f')
                    except ValueError:
                        try:
                            submitted_at = datetime.datetime.strptime(complaint['submitted_at'], '%Y-%m-%d %H:%M:%S')
                        except ValueError:
                            submitted_at = datetime.datetime.now()
                else:
                    submitted_at = complaint['submitted_at'] or datetime.datetime.now()

                comp_dict['submitted_at'] = submitted_at

                # Handle image data
                if complaint.get('image'):
                    try:
                        comp_dict['image'] = base64.b64encode(complaint['image']).decode('utf-8')
                    except:
                        comp_dict['image'] = None
                else:
                    comp_dict['image'] = None

                processed_complaints.append(comp_dict)
            except Exception as e:
                app.logger.error(f"Error processing complaint {complaint.get('id', 'unknown')}: {str(e)}")
                continue
        
    return render_template('admin_dashboard.html', 
                         complaints=processed_complaints,
                         search_id=search_id)

@app.route('/update_complaint_status/<int:complaint_id>', methods=['POST'])
@login_required
def update_complaint_status(complaint_id):
    if not session.get('is_admin'):
        return redirect(url_for('index'))
    status = request.form.get('status')
    remarks = request.form.get('remarks')
    if not status or not remarks:
        flash('Status and remarks are required.', 'error')
        return redirect(url_for('admin_dashboard'))
    with sqlite3.connect(APP_DB) as conn:
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('UPDATE complaints SET status = ?, remarks = ? WHERE id = ?', 
                    (status, remarks, complaint_id))
        conn.commit()
        
        # Create initial chat message for the status update
        try:
            chat_ref = db.reference(f'chats/{complaint_id}/messages')
            if chat_ref.get() is None:
                chat_ref.push({
                    'text': f"Status updated to: {status}\nRemarks: {remarks}",
                    'sender_id': 'admin',
                    'sender_name': 'Admin',
                    'timestamp': datetime.datetime.utcnow().isoformat() + "Z"
                })
                app.logger.info(f"Initial chat message for complaint #{complaint_id} created.")
        except Exception as e:
            app.logger.error(f"Failed to create initial Firebase chat message: {e}")
    
    flash('Complaint status updated successfully.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/delete_complaint/<int:complaint_id>', methods=['POST'])
@login_required
def delete_complaint(complaint_id):
    if not session.get('is_admin'):
        app.logger.warning(f"Non-admin user {session.get('user_id')} attempted to delete complaint {complaint_id}.")
        return jsonify({'error': 'Unauthorized access.'}), 403

    try:
        with sqlite3.connect(APP_DB) as conn:
            conn.execute('PRAGMA foreign_keys = ON') # Ensure cascading deletes work
            cursor = conn.cursor()
            
            # Check if the complaint exists before deleting
            result = cursor.execute('SELECT id FROM complaints WHERE id = ?', (complaint_id,)).fetchone()
            if not result:
                return jsonify({'error': 'Complaint not found.'}), 404
            
            # Delete the complaint. The ON DELETE CASCADE on the upvotes table will handle related upvotes.
            cursor.execute('DELETE FROM complaints WHERE id = ?', (complaint_id,))
            conn.commit()
            
            app.logger.info(f"Admin {session.get('username')} deleted complaint #{complaint_id}.")
            flash('Complaint deleted successfully.', 'success')
            return jsonify({'success': True, 'message': 'Complaint deleted successfully.'})

    except sqlite3.Error as e:
        app.logger.error(f"Database error while deleting complaint {complaint_id}: {e}")
        return jsonify({'error': 'Database operation failed.', 'details': str(e)}), 500

# --- Public & User Complaint Routes ---
@app.route('/pothole_stats')
def pothole_stats():
    """Return pothole statistics"""
    with sqlite3.connect(APP_DB) as conn:
        conn.row_factory = dict_factory
        result = conn.execute('''
            SELECT 
                CAST(total_potholes AS INTEGER) as total_potholes,
                CAST(high_priority_count AS INTEGER) as high_priority_count,
                CAST(medium_priority_count AS INTEGER) as medium_priority_count,
                CAST(low_priority_count AS INTEGER) as low_priority_count,
                last_updated
            FROM pothole_stats 
            WHERE id = 1
        ''').fetchone()

        if result:
            try:
                processed = {
                    'total_potholes': int(result['total_potholes'] or 0),
                    'high_priority_count': int(result['high_priority_count'] or 0),
                    'medium_priority_count': int(result['medium_priority_count'] or 0),
                    'low_priority_count': int(result['low_priority_count'] or 0),
                    'last_updated': result['last_updated'].isoformat() if result['last_updated'] else datetime.datetime.now().isoformat()
                }
                return jsonify(processed)
            except Exception as e:
                app.logger.error(f"Error processing pothole stats: {str(e)}")
    
    # Return default values if no stats or error
    return jsonify({
        'total_potholes': 0,
        'high_priority_count': 0,
        'medium_priority_count': 0,
        'low_priority_count': 0,
        'last_updated': datetime.datetime.now().isoformat()
    })

@app.route('/complaints')
@login_required
def view_complaints():
    sort_by = request.args.get('sort', 'time_desc')
    search_id = request.args.get('search_id', type=int)  # Get the search_id as an integer

    # --- Start of new logic ---
    order_clause = "ORDER BY submitted_at DESC"
    if sort_by == 'upvotes_desc':
        order_clause = "ORDER BY upvotes DESC, submitted_at DESC"
    elif sort_by == 'time_asc':
        order_clause = "ORDER BY submitted_at ASC"

    # Dynamically build the WHERE clause and parameters to prevent SQL injection
    where_conditions = ["(c.is_duplicate = 0 OR c.is_duplicate IS NULL)"]
    params = []

    if search_id:
        where_conditions.append("c.id = ?")
        params.append(search_id)
    
    where_clause = "WHERE " + " AND ".join(where_conditions)
    # --- End of new logic ---
    
    with sqlite3.connect(APP_DB) as conn:
        conn.row_factory = dict_factory
        
        # Build the final query
        query = f'''
            SELECT 
                c.id, c.text, 
                CAST(c.location_lat AS FLOAT) as location_lat,
                CAST(c.location_lon AS FLOAT) as location_lon,
                c.issue_type, c.image, c.submitted_at, c.status,
                c.upvotes, c.remarks, c.is_duplicate, c.original_report_id,
                u.username 
            FROM complaints c
            LEFT JOIN users u ON c.user_id = u.id
            {where_clause}
            {order_clause}
        '''
        
        complaints_raw = conn.execute(query, params).fetchall()
        
        # Process and validate each complaint
        processed_complaints = []
        for complaint in complaints_raw:
            try:
                # Create a clean dictionary with basic complaint info
                comp_dict = {
                    'id': int(complaint['id']),
                    'text': str(complaint['text'] or ''),
                    'location_lat': float(complaint['location_lat'] or 0),
                    'location_lon': float(complaint['location_lon'] or 0),
                    'issue_type': str(complaint['issue_type'] or ''),
                    'status': str(complaint['status'] or 'Submitted'),
                    'upvotes': int(complaint['upvotes'] or 0),
                    'remarks': str(complaint['remarks'] or ''),
                    'username': str(complaint['username'] or ''),
                    'is_duplicate': bool(complaint.get('is_duplicate')),
                    'original_report_id': int(complaint['original_report_id']) if complaint.get('original_report_id') else None
                }

                # Handle datetime
                if isinstance(complaint['submitted_at'], str):
                    try:
                        submitted_at = datetime.datetime.strptime(complaint['submitted_at'], '%Y-%m-%d %H:%M:%S.%f')
                    except ValueError:
                        try:
                            submitted_at = datetime.datetime.strptime(complaint['submitted_at'], '%Y-%m-%d %H:%M:%S')
                        except ValueError:
                            submitted_at = datetime.datetime.now()
                else:
                    submitted_at = complaint['submitted_at'] or datetime.datetime.now()

                comp_dict['submitted_at'] = submitted_at

                # Handle image data
                if complaint.get('image'):
                    try:
                        comp_dict['image'] = base64.b64encode(complaint['image']).decode('utf-8')
                    except:
                        comp_dict['image'] = None
                else:
                    comp_dict['image'] = None

                processed_complaints.append(comp_dict)
            except Exception as e:
                app.logger.error(f"Error processing complaint {complaint.get('id', 'unknown')}: {str(e)}")
                continue
    
    # Pass search_id back to the template
    return render_template('complaints.html', 
                         complaints=processed_complaints, 
                         sort_by=sort_by,
                         search_id=search_id)

@app.route('/upvote_complaint/<int:complaint_id>', methods=['POST'])
@login_required
def upvote_complaint(complaint_id):
    if session.get('is_admin'):
        return jsonify({'error': 'Admins cannot upvote.'}), 403

    user_id = session['user_id']

    with sqlite3.connect(APP_DB) as conn:
        conn.execute('PRAGMA foreign_keys = ON')
        conn.row_factory = dict_factory
        cursor = conn.cursor()

        # --- START: CORRECTED AND MORE ROBUST LOGIC ---

        # Step 1: Explicitly check if the user has already upvoted this specific complaint.
        existing_vote = cursor.execute(
            'SELECT id FROM upvotes WHERE user_id = ? AND complaint_id = ?',
            (user_id, complaint_id)
        ).fetchone()

        if existing_vote:
            # If a vote record already exists, inform the user and stop.
            app.logger.warning(f"User {user_id} attempted to upvote complaint {complaint_id} again.")
            return jsonify({'error': 'You have already upvoted this complaint.'}), 409 # HTTP 409 Conflict

        # Step 2: If no vote exists, proceed with the transaction.
        # The 'with' block automatically handles the transaction commit/rollback.
        try:
            # Insert the new upvote record to enforce the one-vote-per-user rule for the future.
            cursor.execute(
                'INSERT INTO upvotes (user_id, complaint_id) VALUES (?, ?)',
                (user_id, complaint_id)
            )
            
            # Now, safely increment the upvote count on the main complaints table.
            cursor.execute(
                'UPDATE complaints SET upvotes = upvotes + 1 WHERE id = ?',
                (complaint_id,)
            )
            
            # The 'with' block will commit the transaction automatically upon exiting the 'try' block.
            
            # Step 3: Fetch the new, updated count to send back to the frontend.
            new_count_result = cursor.execute('SELECT upvotes FROM complaints WHERE id = ?', (complaint_id,)).fetchone()
            
            if new_count_result:
                return jsonify({'success': True, 'new_count': new_count_result['upvotes']})
            else:
                # This is an edge case (complaint deleted mid-request), but it's good practice to handle.
                # The 'with' block will roll back the transaction if we raise an exception.
                raise Exception("Complaint not found after upvoting.")

        except sqlite3.Error as e:
            # The 'with' block automatically rolls back the transaction on any exception.
            app.logger.error(f"Database error during upvote transaction for complaint {complaint_id} by user {user_id}: {e}")
            return jsonify({'error': 'A database error occurred during the upvote process.'}), 500

        # --- END: CORRECTED LOGIC ---

# --- Chat Routes ---

@app.route('/chat/unread_counts', methods=['GET'])
@login_required
def get_unread_counts():
    """
    Calculates unread message counts for all relevant chats for the current user.
    """
    try:
        user_id = session.get('user_id')
        is_admin = session.get('is_admin', False)
        
        # Get all chats from Firebase
        all_chats = db.reference('chats').get()
        if not all_chats:
            return jsonify({})

        unread_counts = {}
        is_list = isinstance(all_chats, list)

        with sqlite3.connect(APP_DB) as conn:
            conn.row_factory = dict_factory
            
            if is_admin:
                complaints = conn.execute('SELECT id FROM complaints').fetchall()
                complaint_ids = {c['id'] for c in complaints}
                participant_id = 'admin'
                
                for comp_id in complaint_ids:
                    chat_data = None
                    if is_list:
                        if comp_id < len(all_chats):
                            chat_data = all_chats[comp_id]
                    else:
                        chat_data = all_chats.get(str(comp_id))

                    if not chat_data or 'messages' not in chat_data:
                        continue
                    
                    last_read = chat_data.get('metadata', {}).get(participant_id, {}).get('last_read', '1970-01-01T00:00:00Z')
                    
                    count = 0
                    for msg in chat_data['messages'].values():
                        if msg.get('sender_id') != participant_id and msg.get('timestamp') > last_read:
                            count += 1
                    if count > 0:
                        unread_counts[comp_id] = count
            else:
                complaints = conn.execute('SELECT id FROM complaints WHERE user_id = ?', (user_id,)).fetchall()
                complaint_ids = {c['id'] for c in complaints}
                participant_id = f"user_{user_id}"

                for comp_id in complaint_ids:
                    chat_data = None
                    if is_list:
                        if comp_id < len(all_chats):
                            chat_data = all_chats[comp_id]
                    else:
                        chat_data = all_chats.get(str(comp_id))

                    if not chat_data or 'messages' not in chat_data:
                        continue

                    last_read = chat_data.get('metadata', {}).get(participant_id, {}).get('last_read', '1970-01-01T00:00:00Z')
                    
                    count = 0
                    for msg in chat_data['messages'].values():
                        if msg.get('sender_id') == 'admin' and msg.get('timestamp') > last_read:
                            count += 1
                    if count > 0:
                        unread_counts[comp_id] = count
                        
        return jsonify(unread_counts)

    except Exception as e:
        app.logger.error(f"Failed to get unread counts: {e}", exc_info=True)
        return jsonify({'error': 'Failed to retrieve unread message counts.'}), 500

@app.route('/chat/<int:complaint_id>/mark_read', methods=['POST'])
@login_required
def mark_chat_as_read(complaint_id):
    """Updates the last_read timestamp for a user/admin in a chat."""
    try:
        # Determine the participant's identifier (e.g., 'admin' or 'user_123')
        if session.get('is_admin'):
            participant_id = 'admin'
        else:
            participant_id = f"user_{session['user_id']}"
            
        # Security check: ensure the user is part of this complaint
        with sqlite3.connect(APP_DB) as conn:
            complaint_owner_id = conn.execute(
                'SELECT user_id FROM complaints WHERE id = ?', (complaint_id,)
            ).fetchone()
        
        if not complaint_owner_id:
            return jsonify({'error': 'Complaint not found.'}), 404
            
        is_owner = (complaint_owner_id[0] == session.get('user_id'))
        if not session.get('is_admin') and not is_owner:
            return jsonify({'error': 'Unauthorized.'}), 403

        # Update the last_read timestamp in Firebase
        now_iso = datetime.datetime.utcnow().isoformat() + "Z"
        metadata_ref = db.reference(f'chats/{complaint_id}/metadata/{participant_id}')
        metadata_ref.update({'last_read': now_iso})
        
        app.logger.info(f"Chat for complaint {complaint_id} marked as read for {participant_id}.")
        return jsonify({'success': True})
        
    except Exception as e:
        app.logger.error(f"Failed to mark chat as read for complaint #{complaint_id}: {e}")
        return jsonify({'error': 'Failed to update read status.'}), 500

@app.route('/chat/<int:complaint_id>/send', methods=['POST'])
@login_required
def send_chat_message(complaint_id):
    data = request.get_json()
    if not data or 'text' not in data:
        return jsonify({'error': 'Message text is required.'}), 400

    # Security check: Ensure sender is either admin or complaint owner
    with sqlite3.connect(APP_DB) as conn:
        conn.row_factory = dict_factory
        complaint = conn.execute('SELECT user_id FROM complaints WHERE id = ?', (complaint_id,)).fetchone()

    if not complaint:
        return jsonify({'error': 'Complaint not found.'}), 404

    is_owner = (complaint['user_id'] == session['user_id'])
    if not session.get('is_admin') and not is_owner:
        return jsonify({'error': 'Unauthorized to send messages to this chat.'}), 403

    # --- START OF CORRECTIONS ---
    
    # Create message data (Corrected logic)
    is_admin_session = session.get('is_admin', False)
    sender_id = 'admin' if is_admin_session else f"user_{session['user_id']}"
    sender_name = 'Admin' if is_admin_session else session.get('username', 'Unknown')
    
    message = {
        'text': data['text'],
        'sender_id': sender_id,
        'sender_name': sender_name,
        'timestamp': datetime.datetime.utcnow().isoformat() + "Z"
    }
    # Removed the duplicated 'message' definition that was here

    try:
        chat_ref = db.reference(f'chats/{complaint_id}/messages')
        chat_ref.push(message)
        return jsonify({'success': True, 'message': 'Message sent.'})
    except Exception as e:
        # ADD THIS CRITICAL LOGGING LINE
        app.logger.error(f"FIREBASE PUSH FAILED for complaint #{complaint_id}: {e}", exc_info=True)
        return jsonify({'error': 'Failed to send message to the database.'}), 500
    # --- END OF CORRECTIONS ---

@app.route('/chat/<int:complaint_id>/clear', methods=['POST'])
@login_required
def clear_chat_history(complaint_id):
    """Clears the chat history for a specific complaint."""
    try:
        # Security check: ensure the user is part of this complaint
        with sqlite3.connect(APP_DB) as conn:
            complaint_owner_id = conn.execute(
                'SELECT user_id FROM complaints WHERE id = ?', (complaint_id,)
            ).fetchone()
        
        if not complaint_owner_id:
            return jsonify({'error': 'Complaint not found.'}), 404
            
        is_owner = (complaint_owner_id[0] == session.get('user_id'))
        if not session.get('is_admin') and not is_owner:
            return jsonify({'error': 'Unauthorized.'}), 403

        # Clear the messages in Firebase
        chat_ref = db.reference(f'chats/{complaint_id}/messages')
        chat_ref.delete()
        
        app.logger.info(f"Chat history for complaint #{complaint_id} cleared by user {session.get('user_id')}.")
        return jsonify({'success': True, 'message': 'Chat history cleared.'})
        
    except Exception as e:
        app.logger.error(f"Failed to clear chat history for complaint #{complaint_id}: {e}")
        return jsonify({'error': 'Failed to clear chat history.'}), 500

@app.route('/chat/<int:complaint_id>/messages', methods=['GET'])
@login_required
def get_chat_messages(complaint_id):
    # Security check: Ensure user is admin or complaint owner
    with sqlite3.connect(APP_DB) as conn:
        conn.row_factory = dict_factory
        complaint = conn.execute('SELECT user_id FROM complaints WHERE id = ?', (complaint_id,)).fetchone()

    if not complaint:
        return jsonify({'error': 'Complaint not found.'}), 404

    is_owner = (complaint['user_id'] == session['user_id'])
    if not session.get('is_admin') and not is_owner:
        return jsonify({'error': 'Unauthorized to view this chat.'}), 403

    try:
        messages = db.reference(f'chats/{complaint_id}/messages').get()
        return jsonify(messages or {})
    except Exception as e:
        app.logger.error(f"Failed to retrieve Firebase messages for complaint #{complaint_id}: {e}")
        return jsonify({'error': 'Failed to retrieve messages.'}), 500

# --- NEW: AI Chatbot Routes ---
@app.route('/chat/ai', methods=['POST'])
@login_required
def ai_chat_handler():
    if not chat_model:
        return jsonify({'error': 'AI model is not configured on the server.'}), 503

    data = request.get_json()
    if not data or 'history' not in data or 'message' not in data:
        return jsonify({'error': 'Invalid request format.'}), 400

    history = data['history']
    user_message = data['message']

    # Format history for Gemini API
    gemini_history = []
    # --- START OF CORRECTION: MORE DIRECTIVE SYSTEM PROMPT ---
    # --- START OF MODIFICATION ---
    # We are explicitly telling the AI to use specific markdown for formatting.
    system_instruction = (
        "You are a helpful AI assistant for eNivaran, a civic issue reporting platform. "
        "Your role is to guide users on how to use the platform. Be concise and friendly. "
        "**Crucially, all of your responses MUST be formatted for easy readability. "
        "Use bullet points (starting with '*') for lists and use a single hash ('# ') for main headings. "
        "Do not use long, continuous paragraphs.** "
        "You can answer questions about reporting issues (like potholes), checking complaint status, "
        "and general platform features. Do not answer questions outside of this scope."
    )
    # --- END OF MODIFICATION ---
    # --- END OF CORRECTION ---
    
    # Process history, combining system instruction
    for i, msg in enumerate(history):
        role = 'user' if msg['sender'] == 'user' else 'model'
        # Prepend system instruction to the first user message
        text = msg['text']
        if i == 0 and role == 'user':
            text = f"{system_instruction}\n\nUSER: {text}"
        
        gemini_history.append({'role': role, 'parts': [{'text': text}]})
    
    try:
        # Start a chat session with the existing history
        chat_session = chat_model.start_chat(history=gemini_history)
        
        # Send the new message
        response = chat_session.send_message(user_message)
        
        # Return the AI's response text
        return jsonify({'response': response.text})
    except Exception as e:
        app.logger.error(f"Gemini API call failed: {e}")
        # Provide a user-friendly error
        return jsonify({'error': 'The AI service is currently unavailable or encountered an error. Please try again later.'}), 500


@app.route('/upload_chat_file', methods=['POST'])
@login_required
def upload_chat_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part in the request.'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected for uploading.'}), 400

    if file:
        filename = secure_filename(file.filename)
        save_path = os.path.join(CHAT_UPLOAD_FOLDER, filename)
        file.save(save_path)
        app.logger.info(f"User {session['user_id']} uploaded chat file: {filename}")
        
        return jsonify({
            'success': True,
            'message': f'File "{filename}" uploaded successfully.',
            'filename': filename
        })
    
    return jsonify({'error': 'File upload failed.'}), 500

# --- NEW: My Complaints Route ---
@app.route('/my_complaints')
@login_required
def my_complaints():
    if session.get('is_admin'):
        flash("Admin users can view all complaints via the admin dashboard.", "info")
        return redirect(url_for('admin_dashboard'))
    
    user_id = session['user_id']
    with sqlite3.connect(APP_DB) as conn:
        conn.row_factory = dict_factory
        complaints_raw = conn.execute('''
            SELECT 
                c.id, 
                c.text, 
                c.issue_type,
                c.image,
                c.submitted_at,
                c.status,
                c.upvotes,
                c.remarks,
                c.is_duplicate,
                c.original_report_id,
                u.username 
            FROM complaints c
            LEFT JOIN users u ON c.user_id = u.id
            WHERE c.user_id = ? 
            ORDER BY c.submitted_at DESC
        ''', (user_id,)).fetchall()
        
        # Process and validate each complaint
        processed_complaints = []
        for complaint in complaints_raw:
            try:
                # Create a clean dictionary with basic complaint info
                comp_dict = {
                    'id': int(complaint['id']),
                    'text': str(complaint['text'] or ''),
                    'issue_type': str(complaint['issue_type'] or ''),
                    'status': str(complaint['status'] or 'Submitted'),
                    'upvotes': int(complaint['upvotes'] or 0),
                    'remarks': str(complaint['remarks'] or ''),
                    'username': str(complaint['username'] or ''),
                    'is_duplicate': bool(complaint['is_duplicate']),
                    'original_report_id': int(complaint['original_report_id']) if complaint['original_report_id'] else None
                }

                # Handle image data
                if complaint.get('image'):
                    try:
                        comp_dict['image'] = base64.b64encode(complaint['image']).decode('utf-8')
                    except:
                        comp_dict['image'] = None
                else:
                    comp_dict['image'] = None

                # Handle datetime
                if isinstance(complaint['submitted_at'], str):
                    try:
                        submitted_at = datetime.datetime.strptime(complaint['submitted_at'], '%Y-%m-%d %H:%M:%S.%f')
                    except ValueError:
                        try:
                            submitted_at = datetime.datetime.strptime(complaint['submitted_at'], '%Y-%m-%d %H:%M:%S')
                        except ValueError:
                            submitted_at = datetime.datetime.now()
                else:
                    submitted_at = complaint['submitted_at'] or datetime.datetime.now()

                comp_dict['submitted_at'] = submitted_at
                processed_complaints.append(comp_dict)
            except Exception as e:
                app.logger.error(f"Error processing complaint {complaint.get('id', 'unknown')}: {str(e)}")
                continue
        
    return render_template('my_complaints.html', 
                         complaints=processed_complaints, 
                         now=datetime.datetime.now())

# --- Complaint Submission ---
@app.route('/raise_complaint', methods=['POST'])
@login_required
def raise_complaint():
    user_id = session['user_id']
    if session.get('is_admin'):
         return jsonify({'error': 'Admin users cannot raise complaints.'}), 403

    form = request.form
    if not all([form.get(k) for k in ['text', 'issue_type', 'street', 'city', 'state', 'zipcode']]) or 'image' not in request.files:
        return jsonify({'error': 'All fields and an image are required.'}), 400

    image_file = request.files['image']
    image_bytes = image_file.read()
    
    lat, lon = get_coordinates_from_address(form['street'], form['city'], form['state'], form['zipcode'])
    if not lat:
        return jsonify({'error': 'Could not find coordinates for the address.'}), 400

    # 1. Create a report dictionary for the detector
    new_report_data = {
        'text': form['text'],
        'location': (lat, lon),
        'issue_type': form['issue_type'],
        'image_bytes': image_bytes
        # We will add the 'id' later if it's not a duplicate
    }
    
    # 2. Check for duplicates using the detector
    is_duplicate, similar_reports, confidence = detector.find_duplicates(new_report_data)
    original_id = None
    
    if is_duplicate and similar_reports:
        # Get the ID of the most similar report
        original_id = similar_reports[0].get('id')
        app.logger.info(f"Duplicate detected with confidence {confidence:.2f}. Original report ID: {original_id}")
    else:
        app.logger.info("No significant duplicate found. Registering as a new complaint.")

    # 3. Save the complaint to the database with the duplication result
    with sqlite3.connect(APP_DB) as conn:
        conn.execute('PRAGMA foreign_keys = ON')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO complaints (
                text, location_lat, location_lon, issue_type,
                image, image_filename, user_id,
                is_duplicate, original_report_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            form['text'], lat, lon, form['issue_type'],
            image_bytes, secure_filename(image_file.filename),
            user_id, 1 if is_duplicate else 0, original_id
        ))
        
        # Get the ID of the newly inserted complaint
        new_complaint_id = cursor.lastrowid
        conn.commit()

    # 4. If it was NOT a duplicate, add it to the detector's in-memory list for future checks
    if not is_duplicate:
        new_report_data['id'] = new_complaint_id  # Add the new ID to the report data
        detector.add_report(new_report_data)
        app.logger.info(f"New complaint #{new_complaint_id} added to the live detector.")

    if is_duplicate:
        return jsonify({'message': f'Complaint registered, but it appears to be a duplicate of report #{original_id}. Your report has been linked.'}), 200
    else:
        return jsonify({'message': 'Complaint registered successfully.'}), 200


# Debug utility routes
@app.route('/debug/reset_complaints', methods=['POST'])
def debug_reset_complaints():
    """Debug route to reset all complaints. Only works in debug mode."""
    if not app.debug:
        return jsonify({'error': 'This route is only available in debug mode'}), 403
    
    try:
        with sqlite3.connect(APP_DB) as conn:
            conn.execute('PRAGMA foreign_keys = OFF')  # Temporarily disable foreign keys
            conn.execute('DELETE FROM complaints')  # Clear all complaints
            conn.execute('DELETE FROM sqlite_sequence WHERE name="complaints"')  # Reset auto-increment
            conn.execute('UPDATE pothole_stats SET total_potholes = 0, high_priority_count = 0, medium_priority_count = 0, low_priority_count = 0')  # Reset stats
            conn.commit()
            flash('All complaints have been cleared successfully.', 'success')
            return jsonify({'message': 'All complaints cleared successfully'}), 200
    except Exception as e:
        app.logger.error(f'Error resetting complaints: {str(e)}')
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)
