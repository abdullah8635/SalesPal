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
    SECRET_KEY='hello',  # Cryptographically secure random key
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=60),
    SESSION_COOKIE_SECURE=False,  # Set to False for local development, True for HTTPS in production
    SESSION_COOKIE_HTTPONLY=True,  # Prevent JavaScript access
    SESSION_COOKIE_SAMESITE='Lax', 
    DB_HOST = 'localhost',
    DB_NAME = 'salespal',
    DB_USER = 'yourusername',
    DB_PASSWORD = 'yourpassword'  # CSRF protection
)

# OPTIMIZED DATABASE POOL CONFIGURATION
db_pool = SimpleConnectionPool(
    minconn=2,      # Reduced from 1
    maxconn=20,     # Reduced from 10000 (this was way too high!)
    host=app.config['DB_HOST'],
    database=app.config['DB_NAME'],
    user=app.config['DB_USER'],
    password=app.config['DB_PASSWORD'],
    keepalives=1,
    keepalives_idle=30,
    keepalives_interval=10,
    keepalives_count=3,
    # Add connection timeout settings
    connect_timeout=10,
    application_name='salespal_app'
)

bcrypt = Bcrypt(app)
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    storage_uri="redis://localhost:6379",
    default_limits=["200 per day", "1000 per hour"]
)

# IMPROVED DATABASE CONNECTION MANAGEMENT
@contextmanager
def get_db_connection():
    conn = None
    try:
        app.logger.debug("Getting database connection from pool")
        conn = db_pool.getconn()
        if conn is None:
            raise Exception("Failed to get connection from pool")
        
        # Test connection
        with conn.cursor() as test_cursor:
            test_cursor.execute("SELECT 1")
        
        yield conn
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
                db_pool.putconn(conn)
                app.logger.debug("Connection returned to pool")
            except Exception as e:
                app.logger.error(f"Error returning connection: {str(e)}")

# Simplified get_db function
def get_db():
    try:
        connection = db_pool.getconn()
        if connection is None:
            raise Exception("No connection available")
        return connection
    except Exception as e:
        app.logger.error(f"Failed to get database connection: {str(e)}")
        return None

def return_db(conn):
    if conn:
        try:
            db_pool.putconn(conn)
        except Exception as e:
            app.logger.error(f"Error returning connection to pool: {str(e)}")

# Add database health check
def check_db_health():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
        return True
    except Exception as e:
        app.logger.error(f"Database health check failed: {str(e)}")
        return False

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
        self.id = str(id)  # Flask-Login needs string ID
        self.name = name
        self.is_admin = is_admin

    def get_id(self):
        return self.id

def init_db():
    try:
        app.logger.debug("Starting database initialization")
        with get_db_connection() as conn:
            if conn is None:
                app.logger.error("Could not establish database connection")
                return False
            
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
        if 'conn' in locals():
            try:
                conn.rollback()
                app.logger.debug("Transaction rolled back successfully")
            except Exception as rollback_error:
                app.logger.error(f"Rollback failed: {str(rollback_error)}")
        return False
      
@app.teardown_appcontext
def close_connection(exception):
    db = g.pop('db', None)
    if db is not None:
        try:
            return_db(db)
            app.logger.debug("Database connection closed")
        except Exception as e:
            app.logger.error(f"Error closing database connection: {e}")

CONNECTION_POOL = None

def init_db_pool(app):
    global CONNECTION_POOL
    try:
        CONNECTION_POOL = SimpleConnectionPool(
            minconn=1,
            maxconn=20,
            host=app.config['DB_HOST'],
            database=app.config['DB_NAME'],
            user=app.config['DB_USER'],
            password=app.config['DB_PASSWORD']
        )
        app.logger.info("Database connection pool initialized successfully")
    except Exception as e:
        app.logger.error(f"Error initializing database connection pool: {e}")
        raise
      
def release_db(conn):
    global CONNECTION_POOL
    try:
        if CONNECTION_POOL and conn:
            CONNECTION_POOL.putconn(conn)
    except Exception as e:
        logging.error(f"Error releasing database connection: {e}")
      
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

app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')

if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

handler = RotatingFileHandler('flask_app.log', maxBytes=10000, backupCount=3)
handler.setLevel(logging.ERROR)
app.logger.addHandler(handler)

@login_manager.user_loader
def load_user(user_id):
    conn = None
    try:
        conn = get_db()
        if conn is None:
            logging.error("Could not establish database connection for user loading")
            return None
        
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, name, is_admin FROM users WHERE id = %s", (user_id,))
            user_data = cursor.fetchone()
            if user_data:
                return User(
                    id=user_data[0],
                    name=user_data[1],
                    is_admin=user_data[2] == 1
                )
        return None
    except Exception as e:
        logging.error(f"Error loading user: {e}")
        return None
    finally:
        if conn:
            release_db(conn)

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
        
        redis_status = True
        try:
            limiter.storage.storage.ping()
        except:
            redis_status = False
        
        status = {
            'status': 'healthy' if db_status and redis_status else 'unhealthy',
            'database': 'ok' if db_status else 'error',
            'redis': 'ok' if redis_status else 'error',
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
            'pool_size': db_pool.maxconn,
            'connections_in_use': db_pool.maxconn - len(db_pool._pool),
            'available_connections': len(db_pool._pool)
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
        db = get_db()
        cursor = db.cursor()
        
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
            db.commit()

        return jsonify({'message': 'Receipt updated successfully'})

    except Exception as e:
        db.rollback()
        print(f"Error updating receipt: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/non_admin_dashboard')
@login_required
def non_admin_dashboard():
    if current_user.is_admin:
        return redirect(url_for('admin_home'))
    
    conn = None
    try:
        conn = get_db()
        if conn is None:
            app.logger.error("Could not establish database connection")
            flash('Database connection error', 'error')
            return render_template('error.html'), 500
        
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
    
    finally:
        if conn:
            release_db(conn)

@app.route('/delete_receipt/<int:receipt_id>', methods=['POST'])
@login_required
def delete_receipt(receipt_id):
    try:
        db = get_db()
        if db is None:
            app.logger.error("Could not establish database connection")
            return "Database connection error", 500
            
        with db.cursor() as cursor:
            cursor.execute("DELETE FROM parsed_receipts WHERE id = %s", (receipt_id,))
            db.commit()
            
        return redirect(url_for('view_receipts'))
        
    except Exception as e:
        if db:
            db.rollback()
        app.logger.error(f"Error deleting receipt {receipt_id}: {str(e)}")
        return "Error deleting receipt", 500
    finally:
        if 'db' in locals():
            db.close()

def round_up(value, decimals=2):
    factor = 10 ** decimals
    return math.ceil(value * factor) / factor

@app.route('/admin/pending_accounts')
@login_required
def pending_accounts():
    if not current_user.is_admin:
        return redirect(url_for('login'))
        
    try:
        db = get_db()
        if db is None:
            app.logger.error("Could not establish database connection")
            return "Database connection error", 500
            
        with db.cursor() as cursor:
            cursor.execute("SELECT id, name, email, phone FROM users WHERE approved = 0")
            pending_users = cursor.fetchall()
            
        return render_template('pending_accounts.html', pending_users=pending_users)
        
    except Exception as e:
        app.logger.error(f"Error fetching pending accounts: {str(e)}")
        return "Error loading pending accounts", 500
    finally:
        if 'db' in locals():
            db.close()

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        phone = request.form['phone']
        
        try:
            password = generate_random_password(10)
            hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
            
            db = get_db()
            if db is None:
                app.logger.error("Could not establish database connection")
                return "Registration service temporarily unavailable", 500
                
            with db.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO users (name, email, phone, password, approved) VALUES (%s, %s, %s, %s, 0)",
                    (name, email, phone, hashed_password)
                )
                db.commit()
                
            return "Your account has been created. Your password is: {}".format(password), 200
            
        except psycopg2.IntegrityError as e:
            db.rollback()
            app.logger.warning(f"Registration failed - duplicate entry: {str(e)}")
            return "Email or phone number already exists", 400
        except Exception as e:
            if 'db' in locals():
                db.rollback()
            app.logger.error(f"Registration error: {str(e)}")
            return "Error during registration", 500
        finally:
            if 'db' in locals():
                db.close()
    
    return render_template('register.html')

def generate_random_password(length, include_special_chars=False):
    characters = string.ascii_letters + string.digits
    if include_special_chars:
        characters += string.punctuation

    password = ''.join(random.choice(characters) for _ in range(length))
    
    return password

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
        
        conn = None
        try:
            conn = get_db()
            if conn is None:
                app.logger.error("Database connection error in login")
                flash("Database connection error", "error")
                return render_template('login.html')
            
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
        finally:
            if conn:
                return_db(conn)
    
    return render_template('login.html')

def reset_user_password(username, new_password):
    try:
        with get_db_connection() as db:
            with db.cursor() as cursor:
                cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
                user = cursor.fetchone()
                
                if not user:
                    app.logger.error(f"Password reset failed: User {username} not found")
                    return False
                
                hashed_password = bcrypt.generate_password_hash(new_password).decode('utf-8')
                
                cursor.execute("""
                    UPDATE users 
                    SET password = %s
                    WHERE username = %s
                    RETURNING id
                """, (hashed_password, username))
                
                updated = cursor.fetchone()
                db.commit()
                
                if updated:
                    app.logger.info(f"Password successfully reset for user: {username}")
                    return True
                else:
                    app.logger.error(f"Password reset failed: No rows updated for {username}")
                    return False
                    
    except Exception as e:
        app.logger.error(f"Password reset error: {str(e)}")
        if 'db' in locals():
            db.rollback()
        return False

@app.route('/reset_password', methods=['POST'])
@limiter.limit("3 per hour")
@login_required
def reset_password_route():
    try:
        data = request.get_json()
        username = data.get('username')
        new_password = data.get('new_password')
        
        if not username or not new_password:
            return jsonify({'error': 'Missing username or new password'}), 400
            
        if reset_user_password(username, new_password):
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
        
    conn = None
    try:
        conn = get_db()
        if conn is None:
            app.logger.error("Could not establish database connection")
            flash("Database connection error", "error")
            return render_template('error.html'), 500
            
        with conn.cursor() as cursor:
            cursor.execute("SELECT name FROM users WHERE id = %s", (current_user.id,))
            user = cursor.fetchone()
            current_username = user[0] if user else 'User'
            
        return render_template('admin_home.html', current_user=current_username)
        
    except Exception as e:
        app.logger.error(f"Error in admin home: {str(e)}")
        flash("An error occurred while loading admin home", "error")
        return render_template('error.html'), 500
    
    finally:
        if conn:
            release_db(conn)

@app.route('/admin/employees')
@login_required
def employee_list():
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.')
        return redirect(url_for('login'))
        
    try:
        db = get_db()
        if db is None:
            app.logger.error("Could not establish database connection")
            return "Database connection error", 500
            
        with db.cursor() as cursor:
            cursor.execute("SELECT id, name, email, phone, username, approved, is_admin, rejected FROM users")
            employees = cursor.fetchall()
            
            cursor.execute("SELECT name FROM users WHERE id = %s", (current_user.id,))
            user = cursor.fetchone()
            current_username = user[0] if user else 'User'
            
        return render_template('employee_list.html', 
                             employees=employees, 
                             current_user=current_username)
                             
    except Exception as e:
        app.logger.error(f"Error in employee list: {str(e)}")
        return "Error loading employee list", 500
    finally:
        if 'db' in locals():
            db.close()

@app.route('/admin/assign_username/<int:user_id>', methods=['POST'])
@login_required
def assign_username(user_id):
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('login'))
    
    username = request.form['username']
    conn = None
    
    try:
        conn = get_db()
        if conn is None:
            flash('Database connection error', 'error')
            return redirect(url_for('employee_list'))
        
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
            existing_user = cursor.fetchone()
            
            if existing_user:
                flash('Username already exists. Please choose another.', 'error')
                return redirect(url_for('employee_list'))
            
            cursor.execute("UPDATE users SET username = %s WHERE id = %s", (username, user_id))
            conn.commit()
            
            flash('Username assigned successfully.', 'success')
            return redirect(url_for('employee_list'))
    
    except Exception as e:
        app.logger.error(f"Error assigning username: {str(e)}")
        flash('An error occurred while assigning username.', 'error')
        return redirect(url_for('employee_list'))
    
    finally:
        if conn:
            release_db(conn)
          
@app.route('/admin/approve/<int:user_id>', methods=['POST'])
@login_required
def approve_account(user_id):
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('login'))
    
    try:
        db = get_db()
        cursor = db.cursor()
        
        cursor.execute("SELECT approved, rejected FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()
        
        if not user:
            flash('User not found.', 'error')
            return redirect(url_for('employee_list'))
        
        if user[0] == 1:
            flash('User account is already approved.', 'info')
            return redirect(url_for('employee_list'))
        
        cursor.execute("UPDATE users SET approved = 1, rejected = 0 WHERE id = %s", (user_id,))
        db.commit()
        
        flash('User account approved successfully.', 'success')
        return redirect(url_for('employee_list'))
    
    except Exception as e:
        app.logger.error(f"Error approving user account: {str(e)}")
        flash('An error occurred while approving the account.', 'error')
        return redirect(url_for('employee_list'))
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'db' in locals():
            db.close()

@app.route('/admin/reject/<int:user_id>', methods=['POST'])
@login_required
def reject_account(user_id):
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('login'))
    
    conn = None
    try:
        conn = get_db()
        if conn is None:
            flash('Database connection error', 'error')
            return redirect(url_for('employee_list'))
        
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE id = %s", (user_id,))
            if not cursor.fetchone():
                flash('User not found.', 'error')
                return redirect(url_for('employee_list'))
            
            cursor.execute("UPDATE users SET rejected = 1, approved = 0 WHERE id = %s", (user_id,))
            conn.commit()
            
            flash('User account rejected successfully.', 'success')
            return redirect(url_for('employee_list'))
    
    except Exception as e:
        app.logger.error(f"Error rejecting user account: {str(e)}")
        flash('An error occurred while rejecting the account.', 'error')
        return redirect(url_for('employee_list'))
    
    finally:
        if conn:
            release_db(conn)

@app.route('/admin/delete/<int:user_id>', methods=['POST'])
@login_required
def delete_account(user_id):
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('login'))
    
    try:
        db = get_db()
        cursor = db.cursor()
        
        cursor.execute("SELECT is_admin FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()
        
        if not user:
            flash('User not found.', 'error')
            return redirect(url_for('employee_list'))
        
        if user[0] == 1:
            flash('Cannot delete another admin account.', 'error')
            return redirect(url_for('employee_list'))
        
        cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
        db.commit()
        
        flash('User account deleted successfully.', 'success')
        return redirect(url_for('employee_list'))
    
    except Exception as e:
        app.logger.error(f"Error deleting user account: {str(e)}")
        flash('An error occurred while deleting the account.', 'error')
        return redirect(url_for('employee_list'))
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'db' in locals():
            db.close()
          
@app.route('/logout')
@login_required
def logout():
    logout_user()
    session.clear()
    flash('You have been logged out successfully.', 'info')
    return redirect(url_for('login'))

# IMPROVED CRICKET PDF PARSING FUNCTIONS
def calculate_accessories_cricket(pdf_text: str) -> Tuple[float, List[float]]:
    """Calculate accessory prices from Cricket receipt text."""
    
    accessory_prices = []
    
    excluded_prefixes = ['DMTK', 'STHN', '60UNL', '55UNL', 'UNLCOR', 'UNLMORE', 'DEFBYOD']
    excluded_keywords = ['Activation Fee', 'Motorola', 'iPhone', 'Samsung', 'SIM', 'Unlimited', 'Cricket More', 'Cricket Core']
    
    lines = pdf_text.split('\n')
    
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        
        if re.match(r'^[A-Z]{3}\d{4}', line):
            item_code = re.match(r'^([A-Z]{3}\d{4})', line).group(1)
            
            if any(item_code.startswith(prefix) for prefix in excluded_prefixes):
                i += 1
                continue
            
            j = i + 1
            item_description = ""
            item_total = 0.0
            
            while j < min(i + 15, len(lines)):
                current_line = lines[j].strip()
                
                if not re.match(r'^\d+\s+@\$', current_line) and not 'Item Total' in current_line:
                    item_description += " " + current_line
                
                if 'Item Total' in current_line:
                    total_match = re.search(r'Item Total\s+\$(\d+\.?\d*)', current_line)
                    if total_match:
                        item_total = float(total_match.group(1))
                        
                        full_description = item_description.lower()
                        is_excluded = any(keyword.lower() in full_description for keyword in excluded_keywords)
                        
                        if not is_excluded and item_total > 0:
                            accessory_prices.append(item_total)
                            app.logger.debug(f"Found accessory: {item_code} - ${item_total} - {item_description.strip()}")
                        else:
                            app.logger.debug(f"Excluded item: {item_code} - ${item_total} - {item_description.strip()}")
                    break
                j += 1
        i += 1
    
    accessory_patterns = [
        r'(HOL\d+|OPE\d+|PRO\d+).*?Item Total\s+\$(\d+\.?\d*)',
        r'(STW\d+|SCR\d+|GLN\d+).*?Item Total\s+\$(\d+\.?\d*)',
        r'(CHG\d+|CBL\d+|CAR\d+).*?Item Total\s+\$(\d+\.?\d*)',
        r'([A-Z]{3}\d{4})(?!.*(?:Motorola|iPhone|Samsung|Unlimited|Cricket|SIM|DEVICE)).*?Item Total\s+\$(\d+\.?\d*)'
    ]
    
    for pattern in accessory_patterns:
        matches = re.finditer(pattern, pdf_text, re.DOTALL | re.IGNORECASE)
        for match in matches:
            if len(match.groups()) >= 2:
                item_code = match.group(1)
                price = float(match.group(2))
                
                if price not in accessory_prices and price > 0:
                    accessory_prices.append(price)
                    app.logger.debug(f"Found accessory (pattern): {item_code} - ${price}")
    
    total_price = round(sum(accessory_prices), 2)
    app.logger.debug(f"Total accessories: ${total_price}, Items: {accessory_prices}")
    
    return total_price, accessory_prices

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

        imei_matches = re.findall(r'IMEI:(\d{15})', pdf_text)
        iccid_matches = re.findall(r'ICCID:(\d{19,20})', pdf_text)
        
        for i, imei in enumerate(imei_matches):
            if i < len(iccid_matches):
                pairs.append({'imei': imei, 'iccid': iccid_matches[i]})
        
        app.logger.debug(f"Found {len(pairs)} IMEI/ICCID pairs")

        activation_fees = re.findall(r'Activation Fee.*?@\$(\d+\.?\d*)', pdf_text, re.DOTALL)
        activations_count = len(activation_fees)
        
        activation_fee_sum = sum(float(fee) for fee in activation_fees if fee)
        
        app.logger.debug(f"Found {activations_count} activations, total fees: ${activation_fee_sum}")

        upgrades_count = 0

        ppp_present = bool(re.search(r'Cricket Protection Plan|Protection Plan', pdf_text, re.IGNORECASE))
        app.logger.debug(f"PPP present: {ppp_present}")

        try:
            accessories_total, accessory_prices_list = calculate_accessories_cricket(pdf_text)
            total_price = accessories_total
            app.logger.debug(f"Calculated accessories total: ${total_price}")
        except Exception as e:
            app.logger.warning(f"Error calculating accessories: {e}")
            total_price = 0.0
            accessory_prices_list = []

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
            app.logger.debug(f"Extracted values - Company: {company_name}, Customer: {customer}, Date: {order_date}, Sales: {sales_person}, Invoice: {rq_invoice}")
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
        app.logger.debug(f"PDF text for debugging: {pdf_text[:2000]}...")
        raise ValueError(f"Error during PDF extraction: {str(e)}")

@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload_pdf():
    current_user_name = 'User'
    try:
        if request.method == 'GET':
            db = get_db()
            if db is None:
                return render_template('upload.html', current_user=current_user_name, error="Database connection error")

            with db.cursor() as cursor:
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
                        'ppp_present': ppp_present,
                        'activation_fee_sum': activation_fee_sum,
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

        db = get_db()
        if db is None:
            app.logger.error("Database connection error")
            return "Database connection error", 500

        with db.cursor() as cursor:
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
                with db.cursor() as cursor:
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
                db.commit()
                app.logger.info(f"Inserted data for {form_data['company_name']} by {logged_in_user}")
            except Exception as e:
                app.logger.error(f"DB insert error: {str(e)}")
                db.rollback()
                return jsonify({'error': 'Database save error'}), 500

            session['current_pdf_index'] = current_index + 1

            if session['current_pdf_index'] >= len(parsed_data_list):
                session.pop('parsed_data_list', None)
                session.pop('current_pdf_index', None)
                return redirect(url_for('view_receipts'))

            return redirect(url_for('confirm_receipt'))

        current_pdf['logged_in_user'] = logged_in_user
        total_pdfs = len(parsed_data_list)
        current_number = current_index + 1

        return render_template('confirm_receipt.html',
                               current_user=logged_in_user,
                               total_pdfs=total_pdfs,
                               current_pdf_number=current_number,
                               **current_pdf)

    except Exception as e:
        app.logger.error(f"Unexpected error in confirm_receipt: {str(e)}")
        return jsonify({'error': 'Unexpected error occurred'}), 500

@app.route('/view_receipts')
@login_required
def view_receipts():
    try:
        db = get_db()
        if db is None:
            flash("Database connection error", "error")
            return redirect(url_for('login'))

        with db.cursor() as cursor:
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
    db = get_db()
    cursor = db.cursor()

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

@app.route('/commission')
@login_required
def commission():
    conn = None
    try:
        conn = get_db()
        if conn is None:
            flash('Database connection error', 'error')
            return render_template('error.html'), 500
        
        with conn.cursor() as cursor:
            cursor.execute("SELECT is_admin FROM users WHERE id = %s", (current_user.id,))
            user = cursor.fetchone()
            is_admin = user and user[0] == 1
            
            if is_admin:
                cursor.execute('''
                    SELECT 
                        users.username, users.name,
                        SUM(COALESCE(parsed_receipts.activations_count, 0)) as total_activations,
                        SUM(COALESCE(parsed_receipts.upgrades_count, 0)) as total_upgrades,
                        SUM(COALESCE(parsed_receipts.activations_count, 0) + COALESCE(parsed_receipts.upgrades_count, 0)) as total_devices,
                        COALESCE(SUM(parsed_receipts.total_price), 0) as total_accessories,
                        CASE 
                            WHEN COALESCE(SUM(parsed_receipts.total_price), 0) >= 1750 THEN 4
                            WHEN COALESCE(SUM(parsed_receipts.total_price), 0) >= 1000 THEN 3
                            WHEN COALESCE(SUM(parsed_receipts.total_price), 0) >= 750 THEN 2
                            WHEN COALESCE(SUM(parsed_receipts.total_price), 0) >= 500 THEN 1
                            ELSE 1
                        END as current_tier
                    FROM users
                    LEFT JOIN parsed_receipts ON users.id = parsed_receipts.user_id
                    WHERE users.is_admin = 0
                    GROUP BY users.username, users.name
                ''')
                commission_data = cursor.fetchall()
                
                return render_template('commission.html', 
                                     commission_data=commission_data, 
                                     is_admin=True,
                                     current_user=current_user.name)
            else:
                cursor.execute('''
                    SELECT 
                        users.username, users.name,
                        SUM(COALESCE(parsed_receipts.activations_count, 0)) as total_activations,
                        SUM(COALESCE(parsed_receipts.upgrades_count, 0)) as total_upgrades,
                        SUM(COALESCE(parsed_receipts.activations_count, 0) + COALESCE(parsed_receipts.upgrades_count, 0)) as total_devices,
                        COALESCE(SUM(parsed_receipts.total_price), 0) as total_accessories,
                        CASE 
                            WHEN COALESCE(SUM(parsed_receipts.total_price), 0) >= 1750 THEN 4
                            WHEN COALESCE(SUM(parsed_receipts.total_price), 0) >= 1000 THEN 3
                            WHEN COALESCE(SUM(parsed_receipts.total_price), 0) >= 750 THEN 2
                            WHEN COALESCE(SUM(parsed_receipts.total_price), 0) >= 500 THEN 1
                            ELSE 1
                        END as current_tier
                    FROM users
                    LEFT JOIN parsed_receipts ON users.id = parsed_receipts.user_id
                    WHERE users.id = %s
                    GROUP BY users.username, users.name
                ''', (current_user.id,))
                commission_data = cursor.fetchall()
                
                accessories_total = commission_data[0][5] if commission_data else 0
                progress = min((float(accessories_total) / 1750 * 100), 100)
                current_tier = commission_data[0][6] if commission_data else 1
                
                return render_template('commission.html', 
                                     commission_data=commission_data, 
                                     is_admin=False,
                                     accessories_total=accessories_total,
                                     current_tier=current_tier,
                                     progress=progress,
                                     current_user=current_user.name)
    
    except Exception as e:
        app.logger.error(f"Error in commission route: {str(e)}")
        flash('An error occurred while retrieving commission data', 'error')
        return render_template('error.html'), 500

finally:
    if conn:
        release_db(conn)

if __name__ == '__main__':
    import sys
    
    initialize_database()
    
    port = int(os.environ.get('PORT', 5000))
    
    debug_mode = os.environ.get('FLASK_ENV') == 'development'
    app.run(
        host='0.0.0.0', 
        port=port,
        debug=debug_mode
    )
