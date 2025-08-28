from flask import Flask, request, abort, session, flash, redirect, url_for, render_template, g, json, jsonify
import re
import traceback
import sys
from flask_bcrypt import Bcrypt
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from limits.storage import RedisStorage
from datetime import timedelta, datetime
from typing import List, Dict, Tuple, Optional
import PyPDF2
from PyPDF2.errors import PdfReadError
import math
import string
import random
import os
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
import mysql.connector
from flask_mysqldb import MySQL
import io
from werkzeug.utils import secure_filename
from werkzeug.exceptions import NotFound
import psycopg2
from psycopg2 import errors
from contextlib import contextmanager
from psycopg2.extras import DictCursor
from psycopg2.pool import SimpleConnectionPool
from functools import wraps
import logging
from logging.handlers import RotatingFileHandler
import atexit
import threading

# Pay period calculation functions
def get_pay_period_start_date():
    """Get the reference start date for pay periods (August 18, 2025)"""
    return datetime(2025, 8, 18)

def get_current_pay_period(reference_date=None):
    """Get the current pay period start and end dates"""
    if reference_date is None:
        reference_date = datetime.now()
    
    pay_period_start = get_pay_period_start_date()
    
    # Calculate how many pay periods have passed since the start date
    days_diff = (reference_date - pay_period_start).days
    
    if days_diff < 0:
        # If we're before the start date, return the first pay period
        period_number = 0
    else:
        period_number = days_diff // 14
    
    # Calculate the actual start and end dates for this pay period
    current_period_start = pay_period_start + timedelta(days=period_number * 14)
    current_period_end = current_period_start + timedelta(days=13)
    
    return current_period_start, current_period_end, period_number

def get_pay_period_by_number(period_number):
    """Get pay period dates for a specific period number"""
    pay_period_start = get_pay_period_start_date()
    current_period_start = pay_period_start + timedelta(days=period_number * 14)
    current_period_end = current_period_start + timedelta(days=13)
    return current_period_start, current_period_end

def format_pay_period_display(start_date, end_date):
    """Format pay period dates for display"""
    return f"{start_date.strftime('%B %d, %Y')} - {end_date.strftime('%B %d, %Y')}"

def parse_order_date(order_date_str):
    """Parse order date string from format like '28-Aug-2024' to datetime"""
    try:
        if not order_date_str or order_date_str == 'N/A':
            return None
        
        # Handle format like "28-Aug-2024" or "28-Aug-2024 10:30 AM"
        date_part = order_date_str.split(' ')[0]  # Take only the date part
        return datetime.strptime(date_part, '%d-%b-%Y')
    except ValueError:
        app.logger.warning(f"Could not parse order date: {order_date_str}")
        return None

def is_date_in_pay_period(order_date_str, period_start, period_end):
    """Check if an order date falls within a pay period"""
    order_date = parse_order_date(order_date_str)
    if order_date is None:
        return False
    
    return period_start <= order_date <= period_end

app = Flask(__name__)
login_manager = LoginManager(app)
login_manager.init_app(app)
login_manager.login_view = 'login'

logging.basicConfig(level=logging.DEBUG, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.FileHandler('app_debug.log'),
                        logging.StreamHandler()
                    ])

app.config.update(
    SECRET_KEY='hello',  # Change this to a secure random key in production
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=60),
    SESSION_COOKIE_SECURE=False,  # Set to True in production with HTTPS
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax', 
    DB_HOST='localhost',
    DB_NAME='salespal',
    DB_USER='yourusername',
    DB_PASSWORD='yourpassword'
)

# SINGLE DATABASE POOL CONFIGURATION
db_pool = None
pool_lock = threading.Lock()

def initialize_db_pool():
    """Initialize the database connection pool with proper error handling"""
    global db_pool
    
    with pool_lock:
        if db_pool is not None:
            return db_pool
            
        try:
            db_pool = SimpleConnectionPool(
                minconn=1,        # Increased minimum connections
                maxconn=5,       # Increased maximum connections  
                host=app.config['DB_HOST'],
                database=app.config['DB_NAME'],
                user=app.config['DB_USER'],
                password=app.config['DB_PASSWORD'],
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=3,
                connect_timeout=10,
                application_name='salespal_app'
            )
            app.logger.info(f"Database pool initialized successfully with {db_pool.minconn}-{db_pool.maxconn} connections")
            return db_pool
            
        except Exception as e:
            app.logger.error(f"Failed to initialize database pool: {str(e)}")
            raise

def close_db_pool():
    """Safely close all database connections"""
    global db_pool
    with pool_lock:
        if db_pool:
            try:
                db_pool.closeall()
                app.logger.info("Database pool closed successfully")
            except Exception as e:
                app.logger.error(f"Error closing database pool: {str(e)}")
            finally:
                db_pool = None

# Register cleanup function
atexit.register(close_db_pool)

@contextmanager
def get_db_connection():
    """Context manager for database connections with proper error handling"""
    conn = None
    try:
        if db_pool is None:
            initialize_db_pool()
            
        app.logger.debug("Getting database connection from pool")
        conn = db_pool.getconn()
        
        if conn is None:
            raise Exception("Failed to get connection from pool")
        
        # Test connection health
        if conn.closed:
            app.logger.warning("Retrieved closed connection, getting new one")
            db_pool.putconn(conn, close=True)
            conn = db_pool.getconn()
            
        # Set autocommit to False for transaction management
        conn.autocommit = False
        
        yield conn
        
    except psycopg2.OperationalError as e:
        app.logger.error(f"Database operational error: {str(e)}")
        if conn:
            try:
                conn.rollback()
            except:
                pass
        raise
    except Exception as e:
        app.logger.error(f"Database connection error: {str(e)}")
        if conn:
            try:
                conn.rollback()
            except:
                pass
        raise
    finally:
        if conn:
            try:
                if not conn.closed:
                    db_pool.putconn(conn)
                    app.logger.debug("Connection returned to pool")
                else:
                    app.logger.warning("Connection was closed, marking as bad")
                    db_pool.putconn(conn, close=True)
            except Exception as e:
                app.logger.error(f"Error returning connection: {str(e)}")

def check_db_health():
    """Check database connection health"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        return True
    except Exception as e:
        app.logger.error(f"Database health check failed: {str(e)}")
        return False

# Initialize database pool on app startup
with app.app_context():
    try:
        initialize_db_pool()
    except Exception as e:
        app.logger.error(f"Failed to initialize database pool on startup: {str(e)}")
        sys.exit(1)

bcrypt = Bcrypt(app)
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    storage_uri="redis://localhost:6379",
    default_limits=["200 per day", "1000 per hour"]
)

@app.errorhandler(404)
def handle_404(e):
    return "Not Found", 404

@app.before_request
def block_suspicious_paths():
    suspicious_paths = [
        '/wp-includes',
        '/xmlrpc.php',
        '/wp-login.php',
        '/feed/'
    ]
    
    for path in suspicious_paths:
        if path in request.path:
            abort(404)
          
@app.errorhandler(500)
def handle_500(e):
    app.logger.error('An error occurred during a request.')
    app.logger.error(traceback.format_exc())
    app.logger.error(f"Exception: {str(e)}")
    app.logger.error(f"Request method: {request.method}")
    app.logger.error(f"Request URL: {request.url}")
    app.logger.error(f"Request data: {request.get_data()}")
    
    return "Internal Server Error", 500

@app.errorhandler(Exception)
def handle_exception(e):
    app.logger.error('Unhandled exception', exc_info=True)   

@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify(error="Rate limit exceeded. Please try again later."), 429

class User(UserMixin):
    def __init__(self, id, name, is_admin):
        self.id = str(id)
        self.name = name
        self.is_admin = is_admin
        self.username = username

    def get_id(self):
        return self.id

def init_db():
    try:
        app.logger.debug("Starting database initialization")
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                app.logger.debug("Setting search path")
                cursor.execute('SET search_path TO public')
                
                app.logger.debug("Creating users table")
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        name VARCHAR(255) UNIQUE NOT NULL,
                        email VARCHAR(255) UNIQUE NOT NULL,
                        phone VARCHAR(20) UNIQUE NOT NULL,
                        username VARCHAR(50) UNIQUE,
                        password TEXT,
                        approved INTEGER DEFAULT 0,
                        is_admin INTEGER DEFAULT 0,
                        rejected INTEGER DEFAULT 0
                    );
                ''')
                
                app.logger.debug("Creating parsed_receipts table")
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS parsed_receipts (
                        id SERIAL PRIMARY KEY,
                        company_name TEXT,
                        customer TEXT,
                        order_date TEXT,
                        sales_person TEXT,
                        rq_invoice TEXT,
                        total_price REAL,
                        accessory_prices TEXT,
                        upgrades_count INTEGER,
                        activations_count INTEGER,
                        ppp_present BOOLEAN,
                        activation_fee_sum REAL,
                        user_id INTEGER,
                        date_submitted TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        imei_iccid_pairs TEXT,
                        FOREIGN KEY(user_id) REFERENCES users(id)
                    );
                ''')
                
                app.logger.debug("Checking for admin user")
                cursor.execute("SELECT * FROM users WHERE username = 'admin'")
                admin_exists = cursor.fetchone()
                
                if not admin_exists:
                    app.logger.debug("Creating admin user")
                    admin_password = bcrypt.generate_password_hash('admin123').decode('utf-8')
                    cursor.execute('''
                        INSERT INTO users (name, email, phone, username, password, approved, is_admin)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ''', ('Admin User', 'admin@example.com', '1234567890', 'admin', admin_password, 1, 1))
                
                conn.commit()
                app.logger.info("Database tables and admin user created successfully")
                return True
                
    except Exception as e:
        app.logger.error(f"Database initialization error: {type(e).__name__}")
        app.logger.error(f"Error details: {str(e)}")
        return False

app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')

if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

handler = RotatingFileHandler('flask_app.log', maxBytes=10000, backupCount=3)
handler.setLevel(logging.ERROR)
app.logger.addHandler(handler)

@login_manager.user_loader
def load_user(user_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id, name, is_admin, username FROM users WHERE id = %s", (user_id,))
                user_data = cursor.fetchone()
                if user_data:
                    return User(
                        id=user_data[0],
                        name=user_data[1],
                        is_admin=user_data[2] == 1,
                        username=user_data[3]  # Add this line
                    )
        return None
    except Exception as e:
        app.logger.error(f"Error loading user: {e}")
        return None

# Redis connection check
try:
    limiter.storage.storage.ping()
    app.logger.info("Successfully connected to Redis")
except Exception as e:
    app.logger.error(f"Failed to connect to Redis: {str(e)}")
    from limits.storage import MemoryStorage
    limiter.storage = MemoryStorage()

DATABASE = '/data/users.db'
os.makedirs('/home/ubuntu/SalesPal/data', exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() == 'pdf'

# PERFORMANCE MONITORING ROUTES
@app.route('/health')
def health_check():
    """Simple health check endpoint"""
    try:
        db_status = check_db_health()
        
        # Check pool status
        pool_status = {
            'total_connections': db_pool.maxconn if db_pool else 0,
            'available_connections': len(db_pool._pool) if db_pool else 0,
            'used_connections': (db_pool.maxconn - len(db_pool._pool)) if db_pool else 0
        }
        
        redis_status = True
        try:
            limiter.storage.storage.ping()
        except:
            redis_status = False
        
        status = {
            'status': 'healthy' if db_status and redis_status else 'unhealthy',
            'database': 'ok' if db_status else 'error',
            'redis': 'ok' if redis_status else 'error',
            'pool': pool_status,
            'timestamp': datetime.now().isoformat()
        }
        
        return jsonify(status), 200 if status['status'] == 'healthy' else 503
        
    except Exception as e:
        return jsonify({
            'status': 'error',
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }), 500

@app.route('/status')
@login_required
def system_status():
    """Detailed system status for admins"""
    if not current_user.is_admin:
        return redirect(url_for('login'))
    
    try:
        pool_info = {
            'pool_size': db_pool.maxconn if db_pool else 0,
            'connections_in_use': (db_pool.maxconn - len(db_pool._pool)) if db_pool else 0,
            'available_connections': len(db_pool._pool) if db_pool else 0
        }
        
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT COUNT(*) as receipt_count, 
                           MAX(date_submitted) as last_submission
                    FROM parsed_receipts 
                    WHERE date_submitted > NOW() - INTERVAL '24 hours'
                """)
                activity = cursor.fetchone()
        
        status = {
            'database_pool': pool_info,
            'recent_activity': {
                'receipts_24h': activity[0] if activity else 0,
                'last_submission': activity[1].isoformat() if activity and activity[1] else None
            }
        }
        
        return jsonify(status)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/update_receipt/<string:rq_invoice>', methods=['POST'])
@login_required
def update_receipt_details(rq_invoice):
    if not request.is_json:
        return jsonify({'error': 'Content-Type must be application/json'}), 400

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT user_id, imei_iccid_pairs
                    FROM parsed_receipts 
                    WHERE rq_invoice = %s
                """, (rq_invoice,))
                
                receipt = cursor.fetchone()
                
                if not receipt:
                    return jsonify({'error': 'Receipt not found'}), 404
                
                updates = request.get_json()

                update_columns = []
                update_values = []

                field_mapping = {
                    'store': 'company_name',
                    'customer': 'customer',
                    'order_date': 'order_date',
                    'sales_person': 'sales_person',
                    'total_price': 'total_price',
                    'accessories': 'accessory_prices',
                    'activation_fee': 'activation_fee_sum',
                    'upgrades': 'upgrades_count',
                    'activations': 'activations_count',
                    'ppp_present': 'ppp_present'
                }

                if 'device_info' in updates:
                    try:
                        for device in updates['device_info']:
                            if not isinstance(device, dict):
                                return jsonify({'error': 'Invalid device info format'}), 400
                            if not all(key in device for key in ['imei', 'iccid']):
                                return jsonify({'error': 'Missing IMEI or ICCID'}), 400
                            if not re.match(r'^\d{15}$', str(device['imei'])):
                                return jsonify({'error': 'IMEI must be exactly 15 digits'}), 400
                            if not re.match(r'^\d{19,20}$', str(device['iccid'])):
                                return jsonify({'error': 'ICCID must be 19-20 digits'}), 400

                        device_info = json.dumps(updates['device_info'])
                        update_columns.append('imei_iccid_pairs = %s')
                        update_values.append(device_info)
                        del updates['device_info']
                    except (TypeError, ValueError) as e:
                        return jsonify({'error': f'Invalid device info format: {str(e)}'}), 400

                for frontend_field, value in updates.items():
                    if frontend_field in field_mapping:
                        db_column = field_mapping[frontend_field]
                        
                        # Special handling for ppp_present to convert to boolean
                        if db_column == 'ppp_present':
                            # Convert various representations to boolean
                            if isinstance(value, bool):
                                boolean_value = value
                            elif isinstance(value, int):
                                boolean_value = bool(value)
                            elif isinstance(value, str):
                                boolean_value = value.lower() in ('true', '1', 'yes', 'on')
                            else:
                                boolean_value = bool(value)
                            
                            update_columns.append(f'{db_column} = %s')
                            update_values.append(boolean_value)
                        else:
                            update_columns.append(f'{db_column} = %s')
                            update_values.append(value)

                if update_columns:
                    update_query = f"""
                        UPDATE parsed_receipts 
                        SET {', '.join(update_columns)}
                        WHERE rq_invoice = %s
                    """
                    update_values.append(rq_invoice)

                    cursor.execute(update_query, tuple(update_values))
                    conn.commit()

        return jsonify({'message': 'Receipt updated successfully'})

    except Exception as e:
        app.logger.error(f"Error updating receipt: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/non_admin_dashboard')
@login_required
def non_admin_dashboard():
    if current_user.is_admin:
        return redirect(url_for('admin_home'))
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT name FROM users WHERE id = %s", (current_user.id,))
                user = cursor.fetchone()
                
                if user:
                    return render_template('non_admin_dashboard.html', current_user=user[0])
                else:
                    return render_template('non_admin_dashboard.html', current_user=current_user.name)
    
    except Exception as e:
        app.logger.error(f"Database error in non_admin_dashboard: {str(e)}")
        flash('An error occurred while loading dashboard', 'error')
        return render_template('error.html'), 500

@app.route('/delete_receipt/<int:receipt_id>', methods=['POST'])
@login_required
def delete_receipt(receipt_id):
    try:
        app.logger.info(f"Delete request for receipt ID: {receipt_id} by user: {current_user.id}")
        
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # First check if receipt exists and user has permission
                cursor.execute("""
                    SELECT user_id, rq_invoice 
                    FROM parsed_receipts 
                    WHERE id = %s
                """, (receipt_id,))
                receipt = cursor.fetchone()
                
                if not receipt:
                    app.logger.warning(f"Receipt {receipt_id} not found")
                    flash('Receipt not found.', 'error')
                    return redirect(url_for('view_receipts'))
                
                # Check permission (user can only delete their own receipts unless admin)
                if not current_user.is_admin and receipt[0] != int(current_user.id):
                    app.logger.warning(f"User {current_user.id} attempted to delete receipt {receipt_id} belonging to user {receipt[0]}")
                    flash('You can only delete your own receipts.', 'error')
                    return redirect(url_for('view_receipts'))
                
                # Delete the receipt
                cursor.execute("DELETE FROM parsed_receipts WHERE id = %s", (receipt_id,))
                
                if cursor.rowcount == 0:
                    app.logger.warning(f"No rows affected when deleting receipt {receipt_id}")
                    flash('Receipt could not be deleted.', 'error')
                else:
                    conn.commit()
                    app.logger.info(f"Successfully deleted receipt {receipt[1]} (ID: {receipt_id})")
                    flash('Receipt deleted successfully.', 'success')
                
        return redirect(url_for('view_receipts'))
        
    except Exception as e:
        app.logger.error(f"Error deleting receipt {receipt_id}: {str(e)}")
        flash('An error occurred while deleting the receipt.', 'error')
        return redirect(url_for('view_receipts'))

def round_up(value, decimals=2):
    factor = 10 ** decimals
    return math.ceil(value * factor) / factor

@app.route('/admin/pending_accounts')
@login_required
def pending_accounts():
    if not current_user.is_admin:
        return redirect(url_for('login'))
        
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id, name, email, phone FROM users WHERE approved = 0")
                pending_users = cursor.fetchall()
                
        return render_template('pending_accounts.html', pending_users=pending_users)
        
    except Exception as e:
        app.logger.error(f"Error fetching pending accounts: {str(e)}")
        return "Error loading pending accounts", 500

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        phone = request.form['phone']
        
        try:
            password = generate_random_password(10)
            hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
            
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO users (name, email, phone, password, approved) VALUES (%s, %s, %s, %s, 0)",
                        (name, email, phone, hashed_password)
                    )
                    conn.commit()
                    
            return "Your account has been created. Your password is: {}".format(password), 200
            
        except psycopg2.IntegrityError as e:
            app.logger.warning(f"Registration failed - duplicate entry: {str(e)}")
            return "Email or phone number already exists", 400
        except Exception as e:
            app.logger.error(f"Registration error: {str(e)}")
            return "Error during registration", 500
    
    return render_template('register.html')

def generate_random_password(length=10, include_special_chars=True):
    """
    Generate a random password with specified length and character types.
    """
    import secrets
    
    # Use more secure character sets
    lowercase = string.ascii_lowercase
    uppercase = string.ascii_uppercase
    digits = string.digits
    special_chars = "!@#$%^&*"
    
    # Ensure at least one character from each type
    password = [
        secrets.choice(lowercase),
        secrets.choice(uppercase), 
        secrets.choice(digits)
    ]
    
    if include_special_chars:
        password.append(secrets.choice(special_chars))
        all_chars = lowercase + uppercase + digits + special_chars
    else:
        all_chars = lowercase + uppercase + digits
    
    # Fill the rest of the password length
    for _ in range(length - len(password)):
        password.append(secrets.choice(all_chars))
    
    # Shuffle the password list to avoid predictable patterns
    secrets.SystemRandom().shuffle(password)
    
    return ''.join(password)
  
# FIXED LOGIN ROUTE
@app.route('/', methods=['GET', 'POST'])
@limiter.limit("20 per minute")
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        if not username or not password:
            flash("Please enter both username and password", "error")
            return render_template('login.html')
        
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT id, name, email, phone, username, password, approved, is_admin 
                        FROM users 
                        WHERE username = %s
                    """, (username,))
                    user_data = cursor.fetchone()
                    
                    app.logger.debug(f"Login attempt for username: {username}")
                    
                    if user_data:
                        user_id, name, email, phone, db_username, hashed_password, approved, is_admin = user_data
                        
                        app.logger.debug(f"User found: {name}, approved: {approved}, is_admin: {is_admin}")
                        
                        if bcrypt.check_password_hash(hashed_password, password):
                            app.logger.debug("Password verified successfully")
                            
                            if approved == 1:
                                app.logger.debug("User is approved, proceeding with login")
                                
                                user = User(
                                    id=user_id,
                                    name=name,
                                    is_admin=is_admin == 1
                                    username=db_username
                                )
                                
                                login_user(user, remember=False)
                                
                                session['logged_in'] = True
                                session['username'] = username
                                session['user_id'] = user_id
                                session['admin'] = is_admin == 1
                                session.permanent = True
                                
                                app.logger.info(f"User {username} logged in successfully")
                                
                                if user.is_admin:
                                    app.logger.debug("Redirecting admin to admin_home")
                                    return redirect(url_for('admin_home'))
                                else:
                                    app.logger.debug("Redirecting regular user to non_admin_dashboard")
                                    return redirect(url_for('non_admin_dashboard'))
                            else:
                                app.logger.warning(f"Login failed for {username}: Account not approved")
                                flash("Account not approved. Please contact administrator.", "error")
                        else:
                            app.logger.warning(f"Login failed for {username}: Invalid password")
                            flash("Invalid username or password", "error")
                    else:
                        app.logger.warning(f"Login failed: Username {username} not found")
                        flash("Invalid username or password", "error")
                        
        except Exception as e:
            app.logger.error(f"Login error: {str(e)}")
            flash("An error occurred during login", "error")
    
    return render_template('login.html')

def reset_user_password(username, new_password):
    """
    Reset a user's password. Returns True on success, False on failure.
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # Check if user exists
                cursor.execute("SELECT id, name FROM users WHERE username = %s", (username,))
                user = cursor.fetchone()
                
                if not user:
                    app.logger.error(f"Password reset failed: User {username} not found")
                    return False
                
                # Hash the new password
                hashed_password = bcrypt.generate_password_hash(new_password).decode('utf-8')
                
                # Update the password
                cursor.execute("""
                    UPDATE users 
                    SET password = %s
                    WHERE username = %s
                    RETURNING id
                """, (hashed_password, username))
                
                updated = cursor.fetchone()
                conn.commit()
                
                if updated:
                    app.logger.info(f"Password successfully reset for user: {username}")
                    return True
                else:
                    app.logger.error(f"Password reset failed: No rows updated for {username}")
                    return False
                    
    except Exception as e:
        app.logger.error(f"Password reset error for {username}: {str(e)}")
        return False
      
@app.route('/admin/generate_password')
@login_required
def generate_password_api():
    if not current_user.is_admin:
        return jsonify({'error': 'Access denied'}), 403
    
    # Generate a secure random password
    password = generate_random_password(10, include_special_chars=True)
    return jsonify({'password': password})
  
@app.route('/reset_password', methods=['POST'])
@limiter.limit("3 per hour")
@login_required
def reset_password_route():
    if not current_user.is_admin:
        return jsonify({'error': 'Access denied. Admin privileges required.'}), 403
    
    try:
        data = request.get_json()
        username = data.get('username', '').strip()
        new_password = data.get('new_password', '').strip()
        
        if not username or not new_password:
            return jsonify({'error': 'Missing username or new password'}), 400
        
        if len(new_password) < 6:
            return jsonify({'error': 'Password must be at least 6 characters long'}), 400
            
        if reset_user_password(username, new_password):
            app.logger.info(f"Admin {current_user.name} reset password for user: {username}")
            return jsonify({'message': 'Password reset successful'}), 200
        else:
            return jsonify({'error': 'Password reset failed'}), 400
            
    except Exception as e:
        app.logger.error(f"Password reset route error: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/home')
@login_required
def home():
    try:
        print("Home route accessed")
        print(f"Session contents: {dict(session)}")
        
        if current_user.is_admin:
            print("Redirecting admin to admin_home")
            return redirect(url_for('admin_home'))
        else:
            print("Redirecting non-admin to non_admin_dashboard")
            return redirect(url_for('non_admin_dashboard'))
    
    except Exception as e:
        app.logger.error(f"Error in home route: {e}")
        flash("An error occurred", "error")
        return redirect(url_for('login'))

@app.route('/admin/home')
@login_required
def admin_home():
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('login'))
        
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT name FROM users WHERE id = %s", (current_user.id,))
                user = cursor.fetchone()
                current_username = user[0] if user else 'User'
            
        return render_template('admin_home.html', current_user=current_username)
        
    except Exception as e:
        app.logger.error(f"Error in admin home: {str(e)}")
        flash("An error occurred while loading admin home", "error")
        return render_template('error.html'), 500

@app.route('/admin/employees')
@login_required  
def employee_list():
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.')
        return redirect(url_for('login'))
        
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # Get all employees with their status
                cursor.execute("""
                    SELECT id, name, email, phone, username, approved, is_admin, rejected 
                    FROM users 
                    ORDER BY 
                        CASE 
                            WHEN approved = 0 AND rejected = 0 THEN 1  -- Pending first
                            WHEN approved = 1 THEN 2                  -- Approved second  
                            WHEN rejected = 1 THEN 3                  -- Rejected last
                        END,
                        name ASC
                """)
                employees = cursor.fetchall()
                
                # Get current user info
                cursor.execute("SELECT name FROM users WHERE id = %s", (current_user.id,))
                user = cursor.fetchone()
                current_username = user[0] if user else 'User'
                
        return render_template('employee_list.html', 
                             employees=employees, 
                             current_user=current_username)
                             
    except Exception as e:
        app.logger.error(f"Error in employee list: {str(e)}")
        flash('An error occurred while loading the employee list.', 'error')
        return redirect(url_for('admin_home'))

@app.route('/admin/assign_username/<int:user_id>', methods=['POST'])
@login_required
def assign_username(user_id):
    if not current_user.is_admin:
        return jsonify({'error': 'Access denied'}), 403
    
    username = request.form.get('username', '').strip()
    
    if not username:
        return jsonify({'error': 'Username cannot be empty'}), 400
    
    if len(username) < 3:
        return jsonify({'error': 'Username must be at least 3 characters long'}), 400
    
    # Basic username validation (alphanumeric and underscore only)
    if not re.match(r'^[a-zA-Z0-9_]+$', username):
        return jsonify({'error': 'Username can only contain letters, numbers, and underscores'}), 400
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # Check if user exists
                cursor.execute("SELECT name FROM users WHERE id = %s", (user_id,))
                user = cursor.fetchone()
                
                if not user:
                    return jsonify({'error': 'User not found'}), 404
                
                # Check if username already exists
                cursor.execute("SELECT id FROM users WHERE username = %s AND id != %s", (username, user_id))
                existing_user = cursor.fetchone()
                
                if existing_user:
                    return jsonify({'error': 'Username already exists. Please choose another.'}), 400
                
                # Assign the username
                cursor.execute("UPDATE users SET username = %s WHERE id = %s", (username, user_id))
                conn.commit()
                
                app.logger.info(f"Admin {current_user.name} assigned username '{username}' to user: {user[0]} (ID: {user_id})")
                return jsonify({'message': 'Username assigned successfully'}), 200
                
    except psycopg2.IntegrityError as e:
        app.logger.error(f"Integrity error assigning username: {str(e)}")
        return jsonify({'error': 'Username already exists'}), 400
    except Exception as e:
        app.logger.error(f"Error assigning username: {str(e)}")
        return jsonify({'error': 'Failed to assign username'}), 500
          
@app.route('/admin/approve/<int:user_id>', methods=['POST'])
@login_required
def approve_account(user_id):
    if not current_user.is_admin:
        return jsonify({'error': 'Access denied'}), 403
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # Check if user exists
                cursor.execute("SELECT name, approved, rejected FROM users WHERE id = %s", (user_id,))
                user = cursor.fetchone()
                
                if not user:
                    return jsonify({'error': 'User not found'}), 404
                
                if user[1] == 1:  # already approved
                    return jsonify({'message': 'User account is already approved'}), 200
                
                # Approve the user
                cursor.execute("UPDATE users SET approved = 1, rejected = 0 WHERE id = %s", (user_id,))
                conn.commit()
                
                app.logger.info(f"Admin {current_user.name} approved user: {user[0]} (ID: {user_id})")
                return jsonify({'message': 'User account approved successfully'}), 200
                
    except Exception as e:
        app.logger.error(f"Error approving user {user_id}: {str(e)}")
        return jsonify({'error': 'Failed to approve user account'}), 500

@app.route('/admin/reject/<int:user_id>', methods=['POST'])
@login_required
def reject_account(user_id):
    if not current_user.is_admin:
        return jsonify({'error': 'Access denied'}), 403
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # Check if user exists
                cursor.execute("SELECT name, approved, rejected FROM users WHERE id = %s", (user_id,))
                user = cursor.fetchone()
                
                if not user:
                    return jsonify({'error': 'User not found'}), 404
                
                # Reject the user
                cursor.execute("UPDATE users SET rejected = 1, approved = 0 WHERE id = %s", (user_id,))
                conn.commit()
                
                app.logger.info(f"Admin {current_user.name} rejected user: {user[0]} (ID: {user_id})")
                return jsonify({'message': 'User account rejected successfully'}), 200
                
    except Exception as e:
        app.logger.error(f"Error rejecting user {user_id}: {str(e)}")
        return jsonify({'error': 'Failed to reject user account'}), 500

@app.route('/admin/delete/<int:user_id>', methods=['POST'])
@login_required
def delete_account(user_id):
    if not current_user.is_admin:
        return jsonify({'error': 'Access denied'}), 403
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # Check if user exists and get user info
                cursor.execute("SELECT is_admin, name FROM users WHERE id = %s", (user_id,))
                user = cursor.fetchone()
                
                if not user:
                    return jsonify({'error': 'User not found'}), 404
                
                # Prevent deleting admin accounts
                if user[0] == 1:
                    return jsonify({'error': 'Cannot delete another admin account'}), 400
                
                # First delete related records (if any) to avoid foreign key constraints
                cursor.execute("DELETE FROM parsed_receipts WHERE user_id = %s", (user_id,))
                
                # Then delete the user
                cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
                
                # Check if deletion was successful
                if cursor.rowcount == 0:
                    return jsonify({'error': 'Failed to delete user account'}), 500
                
                conn.commit()
                app.logger.info(f"Admin {current_user.name} deleted user account: {user[1]} (ID: {user_id})")
                return jsonify({'message': f'User account for {user[1]} deleted successfully'}), 200
                
    except psycopg2.Error as e:
        app.logger.error(f"Database error deleting user {user_id}: {str(e)}")
        return jsonify({'error': 'Database error occurred while deleting the account'}), 500
    except Exception as e:
        app.logger.error(f"Unexpected error deleting user {user_id}: {str(e)}")
        return jsonify({'error': 'An unexpected error occurred while deleting the account'}), 500

@app.route('/admin/get_password/<int:user_id>')
@login_required
def get_user_password(user_id):
    if not current_user.is_admin:
        return jsonify({'error': 'Access denied'}), 403
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT username, password FROM users WHERE id = %s", (user_id,))
                user = cursor.fetchone()
                
                if not user:
                    return jsonify({'error': 'User not found'}), 404
                
                # Since passwords are hashed, we'll need to generate a new one
                # and show it to the admin. This is a security consideration.
                # Alternative: show that password is encrypted and allow reset only
                
                return jsonify({
                    'success': True,
                    'password': '[ENCRYPTED - Use Reset to Change]',
                    'username': user[0]
                })
                
    except Exception as e:
        app.logger.error(f"Error fetching password for user {user_id}: {str(e)}")
        return jsonify({'error': 'Failed to fetch password'}), 500
      
@app.route('/logout')
@login_required
def logout():
    logout_user()
    session.clear()
    flash('You have been logged out successfully.', 'info')
    return redirect(url_for('login'))

# IMPROVED CRICKET PDF PARSING FUNCTIONS
def calculate_accessories_cricket(pdf_text: str) -> Tuple[float, List[float]]:
    """Calculate accessory prices from Cricket receipt text - commissionable amounts only (after discount, before tax)."""
    
    accessory_prices = []
    
    # Expanded list of excluded prefixes for non-accessory items
    excluded_prefixes = [
        'DMTK', 'STHN', 'SGMN', 'SSGN',  # SIM cards
        '60UNL', '55UNL', 'UNLCOR', 'UNLMORE',  # Service plans
        'DEFBYOD', 'BYOD',  # BYOD related
        'DAPN', 'DSMK', 'DMTK', 'DSAM',  # Device prefixes (phones)
        'IQCRC'  # eSIM
    ]
    
    # Expanded list of keywords that indicate non-accessory items
    excluded_keywords = [
        'Activation Fee', 'Upgrade Fee', 'Lease', 'Initial Payment',
        'Motorola', 'iPhone', 'Samsung', 'Galaxy', 'Nokia', 'TCL',
        'SIM', 'eSIM', 'DEVICE SIM', 'BYOD SIM',
        'Unlimited', 'Cricket More', 'Cricket Core', 
        'Cricket Protect', 'Protection Plan',
        'E911', 'Service Fee'
    ]
    
    # Known accessory prefixes (positive indicators)
    accessory_prefixes = [
        'STW', 'HOL', 'OPE', 'PRO', 'CHG', 'CBL', 'CAR', 
        'SCR', 'GLN', 'CAB', 'CAC', 'CAD', 'CAT',
        'CABTAB', 'CACAOB', 'CADKQB', 'CATCQB'
    ]
    
    # Accessory keywords (positive indicators)
    accessory_keywords = [
        'Case', 'Cover', 'Tempered Glass', 'Screen Protector',
        'Charger', 'Cable', 'Adapter', 'Mount', 'Holder',
        'Airpods', 'Earbuds', 'Headphones', 'Speaker',
        'Power Bank', 'Battery', 'Otter', 'COMMUTER', 'OPERATOR',
        'Quikcell', 'CHARGE & SYNC', 'Wall Charger', 'Car Charger',
        'USB-C', 'Lightning', 'Multi-Port'
    ]
    
    lines = pdf_text.split('\n')
    
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        
        # Look for item codes (pattern like ABC1234 or longer codes)
        item_match = re.match(r'^([A-Z]{2,}[A-Z0-9]*)(?:\s*\d{2,})?', line)
        
        if item_match:
            item_code = item_match.group(1).upper()
            
            # Quick exclusion check
            if item_code and any(item_code.strip().upper().startswith(prefix) for prefix in excluded_prefixes):
                i += 1
                continue
            
            # Gather information about this item
            j = i + 1
            item_description = ""
            original_price = 0.0
            discount = 0.0
            item_total = 0.0
            has_imei = False
            has_iccid = False
            found_item_total = False
            
            # Look ahead to gather all item information
            while j < min(i + 20, len(lines)):  # Extended range to catch all details
                current_line = lines[j].strip()
                
                # Check for IMEI/ICCID (excludes this from being an accessory)
                if 'IMEI:' in current_line:
                    has_imei = True
                if 'ICCID:' in current_line:
                    has_iccid = True
                
                # Capture the description (usually on the next line after item code)
                if j == i + 1 and not re.match(r'^\d+\s*@\$', current_line) and current_line:
                    item_description = current_line
                elif j == i + 2 and not item_description and not re.match(r'^\d+\s*@\$', current_line):
                    # Sometimes description spans multiple lines
                    item_description += " " + current_line
                
                # Capture original price (format: 1 @$XX.XX)
                price_match = re.search(r'(\d+)\s*@\$(\d+\.?\d*)', current_line)
                if price_match and original_price == 0:
                    original_price = float(price_match.group(2))
                
                # Capture discount (could be negative value or in Discounts section)
                if 'Discount' in current_line:
                    discount_match = re.search(r'-?\$(\d+\.?\d*)', current_line)
                    if discount_match:
                        discount = float(discount_match.group(1))
                
                # Alternative discount format
                discount_match = re.search(r'^-\$(\d+\.?\d*)', current_line)
                if discount_match:
                    discount = float(discount_match.group(1))

                app.logger.debug(f"[DEBUG] Checking for item total in line: {current_line}")

                # Capture Item Total
                if 'Item Total' in current_line:
                    total_match = re.search(r'Item Total\s*\$?(\d+\.?\d*)', current_line)
                    if total_match:
                        app.logger.debug(f"[DEBUG] Checking for item total in line: {current_line}")
                        item_total = float(total_match.group(1))
                        found_item_total = True
                        
                        # Now determine if this is an accessory
                        full_description = (item_code + " " + item_description).lower()
                        
                        # Exclude if it has IMEI/ICCID
                        if has_imei or has_iccid:
                            break
                        
                        # Exclude if it matches excluded keywords
                        is_excluded = any(keyword.lower() in full_description for keyword in excluded_keywords)
                        if is_excluded:
                            break
                        
                        # Include if it matches accessory prefixes or keywords
                        is_accessory = False
                        
                        # Check positive indicators
                        if any(item_code.startswith(prefix) for prefix in accessory_prefixes):
                            is_accessory = True
                        elif any(keyword.lower() in full_description for keyword in accessory_keywords):
                            is_accessory = True
                        
                        if is_accessory and original_price > 0:
                            # Calculate commissionable price (after discount, before tax)
                            commissionable_price = original_price - discount
                            
                            # Verify against item total if needed (item total includes tax)
                            # We want the pre-tax amount
                            if commissionable_price > 0:
                                accessory_prices.append(commissionable_price)
                                app.logger.debug(f"Found accessory: {item_code} - {item_description}")
                                app.logger.debug(f"  Original: ${original_price}, Discount: ${discount}, Commissionable: ${commissionable_price}")
                        
                        break
                
                j += 1
        i += 1
    
    # Also check for accessories in a simpler format (when structured differently)
    # Pattern: ITEM_CODE\nDescription\n1 @$XX.XX $XX.XX\nTaxes\n...\nItem Total $XX.XX
    pattern = r'([A-Z]{2,}[A-Z0-9]{4,})\s*\n([^\n]+?)\s*\n.*?(\d+)\s*@\$(\d+\.?\d*)\s+\$\d+\.?\d*(?:.*?Discounts?\s*\n.*?-\$(\d+\.?\d*))?.*?Item Total\s+\$(\d+\.?\d*)'
    
    matches = re.finditer(pattern, pdf_text, re.DOTALL)
    for match in matches:
        item_code = match.group(1)
        description = match.group(2)
        quantity = int(match.group(3))
        original_price = float(match.group(4))
        discount = float(match.group(5)) if match.group(5) else 0.0
        
        # Skip if already processed
        commissionable = original_price - discount
        if commissionable in accessory_prices:
            continue
        
        # Check if this section contains IMEI or ICCID
        section_text = match.group(0)
        if 'IMEI:' in section_text or 'ICCID:' in section_text:
            continue
        
        # Apply exclusion rules
        if any(item_code.startswith(prefix) for prefix in excluded_prefixes):
            continue
        
        full_text = (item_code + " " + description).lower()
        if any(keyword.lower() in full_text for keyword in excluded_keywords):
            continue
        
        # Check for positive accessory indicators
        is_accessory = False
        if any(item_code.startswith(prefix) for prefix in accessory_prefixes):
            is_accessory = True
        elif any(keyword.lower() in full_text for keyword in accessory_keywords):
            is_accessory = True
        
        if is_accessory and commissionable > 0:
            accessory_prices.append(commissionable)
            app.logger.debug(f"Found accessory (pattern): {item_code} - {description} - Commissionable: ${commissionable}")
    
    # Remove duplicates and calculate total
    unique_prices = list(set(accessory_prices))
    total_price = round(sum(unique_prices), 2)
    
    app.logger.debug(f"Total accessories (commissionable): ${total_price}, Items: {unique_prices}")
    
    return total_price, unique_prices

def extract_info_from_pdf(file_stream):
    try:
        reader = PyPDF2.PdfReader(file_stream)
        pdf_text = "".join(page.extract_text() or "" for page in reader.pages)
        
        app.logger.debug(f"PDF text preview: {pdf_text[:1000]}...")
        
        company_name = None
        customer = None
        order_date = None
        sales_person = None
        rq_invoice = None
        total_price = 0.0
        upgrades_count = 0
        activations_count = 0
        ppp_present = False
        activation_fee_sum = 0.0
        pairs = []

        # Extract basic information
        store_match = re.search(r'(\d+):\s*([A-Za-z\s]+)', pdf_text)
        if store_match:
            company_name = f"{store_match.group(1)}: {store_match.group(2).strip()}"
            app.logger.debug(f"Found company: {company_name}")

        customer_match = re.search(r'Customer\s*\n\s*([A-Z\s]+)', pdf_text)
        if customer_match:
            customer = customer_match.group(1).strip()
            app.logger.debug(f"Found customer: {customer}")

        order_date_match = re.search(r'Order Date\s*\n\s*([0-9]{1,2}-[A-Za-z]{3}-[0-9]{4}.*)', pdf_text)
        if order_date_match:
            order_date = order_date_match.group(1).strip()
            app.logger.debug(f"Found order date: {order_date}")

        sales_person_match = re.search(r'(?:Sales Person|Tendered By):\s*\n?\s*([A-Z\s]+)', pdf_text)
        if sales_person_match:
            sales_person = sales_person_match.group(1).strip()
            app.logger.debug(f"Found sales person: {sales_person}")

        rq_invoice_match = re.search(r'R(\d+)', pdf_text)
        if rq_invoice_match:
            rq_invoice = f"R{rq_invoice_match.group(1)}"
            app.logger.debug(f"Found RQ invoice: {rq_invoice}")

        # IMPROVED IMEI/ICCID EXTRACTION - Find unique pairs
        imei_iccid_pairs = extract_unique_imei_iccid_pairs(pdf_text)
        pairs = imei_iccid_pairs
        app.logger.debug(f"Found {len(pairs)} unique IMEI/ICCID pairs")

        # IMPROVED ACTIVATION FEE CALCULATION
        activation_fee_details = extract_activation_fees(pdf_text)
        activation_fee_sum = activation_fee_details['average']
        activations_count = activation_fee_details['count']
        
        app.logger.debug(f"Activation fees: {activation_fee_details['individual_fees']}")
        app.logger.debug(f"Average activation fee: ${activation_fee_sum}")
        app.logger.debug(f"Total activations: {activations_count}")

        # Upgrade fees (separate from activation fees)
        upgrade_fees = re.findall(r'Upgrade Fee\s*(\d+)?\s*@\$(\d+\.?\d*)', pdf_text, re.IGNORECASE)
        upgrades_count = 0
        for qty_str, price_str in upgrade_fees:
            qty = int(qty_str) if qty_str else 1
            upgrades_count += qty

        app.logger.debug(f"Found {upgrades_count} upgrades")

        # PPP Detection
        ppp_present = bool(re.search(r'Cricket Protection Plan|Protection Plan|PROTECTON', pdf_text, re.IGNORECASE))
        app.logger.debug(f"PPP present: {ppp_present}")

        # Calculate accessories
        try:
            accessories_total, accessory_prices_list = calculate_accessories_cricket(pdf_text)
            total_price = accessories_total
            app.logger.debug(f"Calculated accessories total: ${total_price}")
        except Exception as e:
            app.logger.warning(f"Error calculating accessories: {e}")
            total_price = 0.0
            accessory_prices_list = []

        # Validation
        required_fields = [company_name, customer, order_date, sales_person, rq_invoice]
        missing_fields = []
        
        if not company_name:
            missing_fields.append("company_name")
        if not customer:
            missing_fields.append("customer")
        if not order_date:
            missing_fields.append("order_date")
        if not sales_person:
            missing_fields.append("sales_person")
        if not rq_invoice:
            missing_fields.append("rq_invoice")

        if missing_fields:
            app.logger.error(f"Missing required fields: {missing_fields}")
            raise ValueError(f"Missing required fields: {', '.join(missing_fields)}")

        return [
            company_name,
            customer,
            order_date,
            sales_person,
            rq_invoice,
            total_price,
            accessory_prices_list,
            upgrades_count,
            activations_count,
            ppp_present,
            pairs,
            activation_fee_sum
        ]

    except Exception as e:
        app.logger.error(f"Error extracting data from PDF: {str(e)}")
        raise ValueError(f"Error during PDF extraction: {str(e)}")
      
def extract_unique_imei_iccid_pairs(pdf_text):
    """
    Extract unique IMEI/ICCID pairs from Cricket receipt text.
    Each device should have a unique IMEI and ICCID pair.
    """
    pairs = []
    
    # Find all IMEI and ICCID numbers
    imei_matches = re.findall(r'IMEI:(\d{15})', pdf_text)
    iccid_matches = re.findall(r'ICCID:(\d{19,20})', pdf_text)
    
    app.logger.debug(f"Raw IMEI matches: {imei_matches}")
    app.logger.debug(f"Raw ICCID matches: {iccid_matches}")
    
    # Remove duplicates while preserving order
    unique_imeis = []
    unique_iccids = []
    
    for imei in imei_matches:
        if imei not in unique_imeis:
            unique_imeis.append(imei)
    
    for iccid in iccid_matches:
        if iccid not in unique_iccids:
            unique_iccids.append(iccid)
    
    app.logger.debug(f"Unique IMEIs: {unique_imeis}")
    app.logger.debug(f"Unique ICCIDs: {unique_iccids}")
    
    # Alternative method: Extract IMEI/ICCID pairs by proximity
    # Look for patterns where IMEI and ICCID appear close together
    lines = pdf_text.split('\n')
    
    for i, line in enumerate(lines):
        imei_match = re.search(r'IMEI:(\d{15})', line)
        if imei_match:
            imei = imei_match.group(1)
            
            # Look for corresponding ICCID in nearby lines (within 10 lines)
            iccid = None
            for j in range(max(0, i-5), min(len(lines), i+10)):
                iccid_match = re.search(r'ICCID:(\d{19,20})', lines[j])
                if iccid_match:
                    potential_iccid = iccid_match.group(1)
                    
                    # Check if this IMEI/ICCID pair is already added
                    pair_exists = any(p['imei'] == imei and p['iccid'] == potential_iccid for p in pairs)
                    if not pair_exists:
                        iccid = potential_iccid
                        break
            
            if iccid:
                # Double-check this pair isn't already in our list
                pair_exists = any(p['imei'] == imei and p['iccid'] == iccid for p in pairs)
                if not pair_exists:
                    pairs.append({'imei': imei, 'iccid': iccid})
                    app.logger.debug(f"Found unique pair: IMEI {imei} -> ICCID {iccid}")
    
    # Fallback: if proximity method didn't work well, pair them sequentially
    if len(pairs) < min(len(unique_imeis), len(unique_iccids)):
        pairs = []
        for i in range(min(len(unique_imeis), len(unique_iccids))):
            pairs.append({'imei': unique_imeis[i], 'iccid': unique_iccids[i]})
    
    app.logger.debug(f"Final unique pairs: {pairs}")
    return pairs


def extract_activation_fees(pdf_text):
    """
    Extract activation fees and calculate average.
    Returns details including individual fees, count, and average.
    Focus on base activation fee amounts, not taxed totals.
    """
    activation_fees = []
    
    # Pattern 1: Look for "Activation Fee" followed by price format
    # Example: "Activation Fee 1 @$25.00 $25.00"
    activation_pattern = re.findall(r'Activation Fee\s+(\d+)?\s*@\$(\d+\.?\d*)', pdf_text, re.IGNORECASE)
    
    for qty_str, price_str in activation_pattern:
        qty = int(qty_str) if qty_str else 1
        base_price = float(price_str)
        
        # Add the base price for each activation (this is the commissionable amount)
        for _ in range(qty):
            activation_fees.append(base_price)
        
        app.logger.debug(f"Found activation fee pattern: {qty} x ${base_price}")
    
    # Pattern 2: If no pattern matches found, look for "Activation Fee" sections manually
    # Based on your PDF, we need to look for lines that just say "Activation Fee" 
    # followed by a price line like "1 @$25.00 $25.00"
    if not activation_fees:
        lines = pdf_text.split('\n')
        
        for i, line in enumerate(lines):
            line = line.strip()
            
            # Look for standalone "Activation Fee" line (exactly this text)
            if line == 'Activation Fee':
                # Look ahead for the price line in the next few lines
                for j in range(i+1, min(len(lines), i+5)):
                    next_line = lines[j].strip()
                    
                    # Look for pattern like "1 @$25.00 $25.00"
                    price_match = re.search(r'(\d+)?\s*@\$(\d+\.?\d*)\s+\$(\d+\.?\d*)', next_line)
                    if price_match:
                        qty = int(price_match.group(1)) if price_match.group(1) else 1
                        base_price = float(price_match.group(2))
                        
                        # Use the base price (@$ amount), not the final amount which might include discounts
                        for _ in range(qty):
                            activation_fees.append(base_price)
                        
                        app.logger.debug(f"Found activation fee (manual): {qty} x ${base_price}")
                        break
    
    # Pattern 3: If still no matches, look for any line containing "Activation Fee" and extract base price
    if not activation_fees:
        lines = pdf_text.split('\n')
        
        for i, line in enumerate(lines):
            if 'Activation Fee' in line:
                # Look in current line and next few lines for @$ pattern
                search_lines = [line] + lines[i+1:i+5]
                
                for search_line in search_lines:
                    price_match = re.search(r'(\d+)?\s*@\$(\d+\.?\d*)', search_line)
                    if price_match:
                        qty = int(price_match.group(1)) if price_match.group(1) else 1
                        base_price = float(price_match.group(2))
                        
                        for _ in range(qty):
                            activation_fees.append(base_price)
                        
                        app.logger.debug(f"Found activation fee (fallback): {qty} x ${base_price}")
                        break
    
    # Don't remove duplicates - keep all activation fees as found
    # This will show all individual fees, including multiple $25.00 fees
    
    # Calculate stats
    if activation_fees:
        count = len(activation_fees)
        average = sum(activation_fees) / count
        
        app.logger.debug(f"Individual activation fees: {activation_fees}")
        app.logger.debug(f"Count: {count}, Average: ${average:.2f}")
        
        return {
            'individual_fees': activation_fees,
            'count': count,
            'average': round(average, 2)
        }
    else:
        return {
            'individual_fees': [],
            'count': 0,
            'average': 0.0
        }
      
@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload_pdf():
    current_user_name = 'User'
    try:
        if request.method == 'GET':
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT name FROM users WHERE id = %s", (current_user.id,))
                    user = cursor.fetchone()
                    current_user_name = user[0] if user else 'User'

            return render_template('upload.html', current_user=current_user_name)

        if 'pdf' not in request.files and 'pdf[]' not in request.files:
            return jsonify({'success': False, 'message': 'No files uploaded'}), 400

        files = request.files.getlist('pdf[]') if 'pdf[]' in request.files else [request.files['pdf']]
        if not any(file and file.filename.strip() for file in files):
            return jsonify({'success': False, 'message': 'No valid files selected'}), 400

        errors = []
        parsed_data_list = []

        for file in files:
            try:
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    content = file.read()
                    if not content:
                        raise ValueError("Empty file")

                    reader = PyPDF2.PdfReader(io.BytesIO(content))
                    if not reader.pages:
                        raise ValueError("PDF has no pages")

                    text = ''.join(page.extract_text() or '' for page in reader.pages)
                    if not text.strip():
                        raise ValueError("PDF contains no text")

                    parsed = extract_info_from_pdf(io.BytesIO(content))
                    (company_name, customer, order_date, sales_person, rq_invoice,
                     total_price, accessories_prices, upgrades_count, activations_count,
                     ppp_present, pairs, activation_fee_sum) = parsed

                    required = [company_name, customer, order_date, sales_person, rq_invoice]
                    if not all(required):
                        raise ValueError("Missing required fields in PDF")

                    # IMPORTANT: Extract activation fee details again to store in session
                    activation_fee_details = extract_activation_fees(text)

                    parsed_data_list.append({
                        'filename': filename,
                        'company_name': company_name,
                        'customer': customer,
                        'order_date': order_date,
                        'sales_person': sales_person,
                        'rq_invoice': rq_invoice,
                        'total_price': total_price,
                        'accessories_prices': accessories_prices,
                        'upgrades_count': upgrades_count,
                        'activations_count': activations_count,
                        'ppp_present': 'ppp_present' in request.form,
                        'activation_fee_sum': activation_fee_sum,
                        'activation_fee_details': activation_fee_details,
                        'imei_iccid_pairs': pairs,
                        'pdf_text': text
                    })

                else:
                    raise ValueError("Invalid file format")
            except Exception as e:
                app.logger.error(f"Error parsing {file.filename}: {str(e)}")
                errors.append(f"{file.filename}: {str(e)}")

        if not parsed_data_list:
            return jsonify({
                'success': False,
                'message': 'Parsing failed for all files',
                'errors': errors
            }), 400

        session['parsed_data_list'] = parsed_data_list
        session['current_pdf_index'] = 0
        session.modified = True

        return jsonify({
            'success': True,
            'message': 'Files uploaded successfully',
            'redirect_url': url_for('confirm_receipt')
        })

    except Exception as e:
        app.logger.error(f"Upload error: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/confirm', methods=['GET', 'POST'])
@login_required
def confirm_receipt():
    try:
        parsed_data_list = session.get('parsed_data_list')
        current_index = session.get('current_pdf_index', 0)

        if not parsed_data_list or current_index is None:
            return redirect(url_for('upload_pdf'))

        if current_index >= len(parsed_data_list):
            session.pop('parsed_data_list', None)
            session.pop('current_pdf_index', None)
            return redirect(url_for('view_receipts'))

        current_pdf = parsed_data_list[current_index]

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT name FROM users WHERE id = %s", (current_user.id,))
                user = cursor.fetchone()
                logged_in_user = user[0] if user else 'User'

        if request.method == 'POST':
            form_data = {
                'company_name': request.form.get('company_name', 'N/A'),
                'customer': request.form.get('customer', 'N/A'),
                'order_date': request.form.get('order_date', 'N/A'),
                'sales_person': request.form.get('sales_person', 'N/A'),
                'rq_invoice': request.form.get('rq_invoice', 'N/A'),
                'total_price': float(request.form.get('total_price', 0)),
                'accessories_prices': request.form.get('accessories_prices', ''),
                'upgrades_count': int(request.form.get('upgrades_count', 0)),
                'activations_count': int(request.form.get('activations_count', 0)),
                'ppp_present': 'ppp_present' in request.form,
                'activation_fee_sum': float(request.form.get('activation_fee_sum', 0))
            }

            imei_iccid_pairs = current_pdf.get('imei_iccid_pairs', [])
            imei_iccid_json = json.dumps(imei_iccid_pairs)

            try:
                with get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute('''
                            INSERT INTO parsed_receipts (
                                company_name, customer, order_date, sales_person, rq_invoice,
                                total_price, accessory_prices, upgrades_count, activations_count,
                                ppp_present, activation_fee_sum, user_id, imei_iccid_pairs
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ''', (
                            form_data['company_name'], form_data['customer'], form_data['order_date'],
                            form_data['sales_person'], form_data['rq_invoice'], form_data['total_price'],
                            form_data['accessories_prices'], form_data['upgrades_count'],
                            form_data['activations_count'], form_data['ppp_present'],
                            form_data['activation_fee_sum'], current_user.id, imei_iccid_json
                        ))
                    conn.commit()
                    app.logger.info(f"Inserted data for {form_data['company_name']} by {logged_in_user}")
            except Exception as e:
                app.logger.error(f"DB insert error: {str(e)}")
                return jsonify({'error': 'Database save error'}), 500

            session['current_pdf_index'] = current_index + 1

            if session['current_pdf_index'] >= len(parsed_data_list):
                session.pop('parsed_data_list', None)
                session.pop('current_pdf_index', None)
                return redirect(url_for('view_receipts'))

            return redirect(url_for('confirm_receipt'))

        # Prepare template data
        template_data = current_pdf.copy()
        template_data['logged_in_user'] = logged_in_user
        template_data['total_pdfs'] = len(parsed_data_list)
        template_data['current_pdf_number'] = current_index + 1

        # Add activation fee details if available
        if 'activation_fee_details' in current_pdf:
            template_data['activation_fee_details'] = current_pdf['activation_fee_details']
        else:
            # Create a simple structure if not available
            template_data['activation_fee_details'] = {
                'individual_fees': [],
                'count': current_pdf.get('activations_count', 0),
                'average': current_pdf.get('activation_fee_sum', 0)
            }

        return render_template('confirm_receipt.html', **template_data)

    except Exception as e:
        app.logger.error(f"Unexpected error in confirm_receipt: {str(e)}")
        return jsonify({'error': 'Unexpected error occurred'}), 500

@app.route('/view_receipts')
@login_required
def view_receipts():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id, name, is_admin FROM users WHERE id = %s", (current_user.id,))
                user = cursor.fetchone()
                
                if not user:
                    flash("User not found", "error")
                    return redirect(url_for('login'))

                user_id, current_user_name, is_admin = user
                app.logger.info(f"User: {current_user_name}, Admin: {is_admin}")

                if is_admin:
                    cursor.execute("""
                        SELECT r.*, u.name AS uploader_name
                        FROM parsed_receipts r
                        LEFT JOIN users u ON r.user_id = u.id
                        ORDER BY r.date_submitted DESC
                    """)
                else:
                    cursor.execute("""
                        SELECT r.*, u.name AS uploader_name
                        FROM parsed_receipts r
                        LEFT JOIN users u ON r.user_id = u.id
                        WHERE r.user_id = %s
                        ORDER BY r.date_submitted DESC
                    """, (user_id,))
                
                receipts = cursor.fetchall()

        return render_template('view_receipts.html', receipts=receipts, current_user=current_user_name)

    except Exception as e:
        app.logger.error(f"Error in view_receipts: {str(e)}")
        flash("An unexpected error occurred", "error")
        return redirect(url_for('login'))

@app.route('/receipt_details/<string:rq_invoice>')
@login_required
def receipt_details(rq_invoice):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id, name, is_admin FROM users WHERE id = %s", (current_user.id,))
                user = cursor.fetchone()
                is_admin = user and user[2] == 1
                current_user_name = user[1] if user else 'User'

                if is_admin:
                    cursor.execute("""
                        SELECT 
                            r.id, r.company_name, r.customer, r.order_date, r.sales_person, r.rq_invoice,
                            r.total_price, r.accessory_prices, r.upgrades_count, r.activations_count,
                            r.ppp_present, r.activation_fee_sum, r.imei_iccid_pairs, u.name as uploader_name
                        FROM parsed_receipts r
                        LEFT JOIN users u ON r.user_id = u.id
                        WHERE r.rq_invoice = %s
                    """, (rq_invoice,))
                else:
                    cursor.execute("""
                        SELECT 
                            r.id, r.company_name, r.customer, r.order_date, r.sales_person, r.rq_invoice,
                            r.total_price, r.accessory_prices, r.upgrades_count, r.activations_count,
                            r.ppp_present, r.activation_fee_sum, r.imei_iccid_pairs, u.name as uploader_name
                        FROM parsed_receipts r
                        LEFT JOIN users u ON r.user_id = u.id
                        WHERE r.rq_invoice = %s AND r.user_id = %s
                    """, (rq_invoice, current_user.id))

                receipt = cursor.fetchone()
                if not receipt:
                    return "Receipt not found", 404
                
                imei_iccid_pairs = []
                if receipt and receipt[12]:
                    try:
                        imei_iccid_pairs = json.loads(receipt[12])
                    except (json.JSONDecodeError, TypeError):
                        pass

        return render_template('receipt_details.html', receipt=receipt, imei_iccid_pairs=imei_iccid_pairs, current_user=current_user_name)
    
    except Exception as e:
        app.logger.error(f"Error in receipt_details: {str(e)}")
        return "Error loading receipt details", 500

@app.route('/commission')
@app.route('/commission/<int:period_offset>')
@login_required
def commission(period_offset=0):
    try:
        # Calculate the target pay period
        current_start, current_end, current_period_number = get_current_pay_period()
        target_period_number = current_period_number + period_offset
        
        # Don't allow negative period numbers
        if target_period_number < 0:
            target_period_number = 0
            period_offset = -current_period_number
        
        period_start, period_end = get_pay_period_by_number(target_period_number)
        
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT is_admin FROM users WHERE id = %s", (current_user.id,))
                user = cursor.fetchone()
                is_admin = user and user[0] == 1
                
                if is_admin:
                    # First get all non-admin users to ensure they all appear in the results
                    cursor.execute('SELECT username, name FROM users WHERE is_admin = 0')
                    all_users = cursor.fetchall()
                    
                    # Initialize user data for all users
                    user_data = {}
                    for username, name in all_users:
                        user_data[username] = [username, name, 0, 0, 0, 0.0, 1]
                    
                    # Get all receipts for filtering
                    cursor.execute('''
                        SELECT 
                            users.username, users.name,
                            parsed_receipts.activations_count,
                            parsed_receipts.upgrades_count,
                            parsed_receipts.total_price,
                            parsed_receipts.order_date
                        FROM users
                        LEFT JOIN parsed_receipts ON users.id = parsed_receipts.user_id
                        WHERE users.is_admin = 0 AND parsed_receipts.order_date IS NOT NULL
                    ''')
                    all_data = cursor.fetchall()
                    
                    # Filter and aggregate data for the pay period
                    for row in all_data:
                        username, name, activations, upgrades, total_price, order_date = row
                        
                        # Check if this receipt is in the current pay period
                        if is_date_in_pay_period(order_date, period_start, period_end):
                            user_data[username][2] += activations or 0  # activations
                            user_data[username][3] += upgrades or 0     # upgrades
                            user_data[username][4] += (activations or 0) + (upgrades or 0)  # total devices
                            user_data[username][5] += total_price or 0.0  # accessories
                    
                    # Calculate tiers
                    commission_data = []
                    for username, data in user_data.items():
                        accessories_total = data[5]
                        if accessories_total >= 1750:
                            tier = 4
                        elif accessories_total >= 1000:
                            tier = 3
                        elif accessories_total >= 750:
                            tier = 2
                        elif accessories_total >= 500:
                            tier = 1
                        else:
                            tier = 1
                        data[6] = tier
                        commission_data.append(data)
                    
                    return render_template('commission.html', 
                                         commission_data=commission_data, 
                                         is_admin=True,
                                         current_user=current_user.name,
                                         period_start=period_start,
                                         period_end=period_end,
                                         period_display=format_pay_period_display(period_start, period_end),
                                         period_offset=period_offset,
                                         is_current_period=(period_offset == 0))
                else:
                    # Get all receipts for the current user
                    cursor.execute('''
                        SELECT 
                            users.username, users.name,
                            parsed_receipts.activations_count,
                            parsed_receipts.upgrades_count,
                            parsed_receipts.total_price,
                            parsed_receipts.order_date
                        FROM users
                        LEFT JOIN parsed_receipts ON users.id = parsed_receipts.user_id
                        WHERE users.id = %s
                    ''', (current_user.id,))
                    all_data = cursor.fetchall()
                    
                    # Filter and aggregate for the pay period
                    total_activations = 0
                    total_upgrades = 0
                    total_accessories = 0.0
                    username = current_user.username
                    name = current_user.name
                    
                    for row in all_data:
                        _, _, activations, upgrades, total_price, order_date = row
                        
                        if order_date and is_date_in_pay_period(order_date, period_start, period_end):
                            total_activations += activations or 0
                            total_upgrades += upgrades or 0
                            total_accessories += total_price or 0.0
                    
                    # Calculate tier
                    if total_accessories >= 1750:
                        current_tier = 4
                    elif total_accessories >= 1000:
                        current_tier = 3
                    elif total_accessories >= 750:
                        current_tier = 2
                    elif total_accessories >= 500:
                        current_tier = 1
                    else:
                        current_tier = 1
                    
                    total_devices = total_activations + total_upgrades
                    commission_data = [[username, name, total_activations, total_upgrades, total_devices, total_accessories, current_tier]]
                    
                    progress = min((float(total_accessories) / 1750 * 100), 100)
                    
                    return render_template('commission.html', 
                                         commission_data=commission_data, 
                                         is_admin=False,
                                         accessories_total=total_accessories,
                                         current_tier=current_tier,
                                         progress=progress,
                                         current_user=current_user.name,
                                         period_start=period_start,
                                         period_end=period_end,
                                         period_display=format_pay_period_display(period_start, period_end),
                                         period_offset=period_offset,
                                         is_current_period=(period_offset == 0))
    
    except Exception as e:
        app.logger.error(f"Error in commission route: {str(e)}")
        flash('An error occurred while retrieving commission data', 'error')
        return render_template('error.html'), 500

def initialize_database():
    try:
        with app.app_context():
            success = init_db()
            if not success:
                app.logger.error("Database initialization failed during startup")
                sys.exit(1)
            app.logger.info("Database initialized successfully during startup")
    except Exception as e:
        app.logger.error(f"Critical error during database initialization: {str(e)}")
        sys.exit(1)

# Call this function when the app starts
with app.app_context():
    if init_db():
        app.logger.info("Database initialized successfully")
    else:
        app.logger.error("Failed to initialize database")

if __name__ == '__main__':
    import sys
    
    port = int(os.environ.get('PORT', 5000))
    
    debug_mode = os.environ.get('FLASK_ENV') == 'development'
    app.run(
        host='0.0.0.0', 
        port=port,
        debug=debug_mode
    )
