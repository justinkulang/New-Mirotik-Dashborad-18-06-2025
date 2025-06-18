"""Mikrotik Hotspot User Management Backend - v2
Major overhaul with a redesigned UI, profile management, filtered exports, QR codes, and more."""
from flask import Flask, render_template, request, jsonify, send_from_directory, g
from flask_cors import CORS
import librouteros
from librouteros.exceptions import TrapError
import socket
import json
import os
import sys
import logging
import random
import string
import re
import math
import io
import csv
from flask import Response
import base64

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Graceful Dependency Handling ---
# Attempt to import WeasyPrint for PDF export
try:
    from weasyprint import HTML as WeasyHTML
    WEASYPRINT_AVAILABLE = True
    logger.info("WeasyPrint library loaded successfully. PDF export is enabled.")
except (ImportError, OSError) as e:
    WEASYPRINT_AVAILABLE = False
    logger.warning("="*50)
    logger.warning("WeasyPrint could not be loaded. PDF export will be disabled.")
    logger.warning(f"Error: {e}")
    logger.warning("This is likely due to missing GTK+ system dependencies.")
    logger.warning("Please follow the installation steps for your OS:")
    logger.warning("https://doc.weasyprint.org/stable/first_steps.html#installation")
    logger.warning("="*50)


# Attempt to import qrcode for QR generation
try:
    import qrcode
    from qrcode.image.styledpil import StyledPilImage
    from qrcode.image.styles.moduledrawers import RoundedModuleDrawer
    QRCODE_AVAILABLE = True
except ImportError:
    QRCODE_AVAILABLE = False
    logger.warning("qrcode library not found. QR codes on vouchers will be disabled.")
    logger.warning("To enable this feature, please install it: pip install qrcode[pil]")


app = Flask(__name__)
CORS(app)


def get_base_path():
    """ Get base path for PyInstaller bundled app or normal script """
    if hasattr(sys, '_MEIPASS'):
        return sys._MEIPASS
    return os.path.abspath(os.path.dirname(__file__))

class ConfigLoader:
    """Handles loading and managing application configuration."""
    def __init__(self, config_file='config.json'):
        self.config_file = os.path.join(get_base_path(), config_file)
        self.config = self._load_config()

    def _load_config(self):
        """Load configuration from config.json or create default if not exists."""
        default_config = {
            "mikrotik": {
                "host": "192.168.88.1",
                "port": 8728,
                "username": "admin",
                "password": "",
                "use_ssl": False,
                "hotspot_login_url": "http://hotspot.setup/login"
            },
            "server": {
                "host": "0.0.0.0",
                "port": 5000,
                "debug": True
            }
        }

        if os.path.exists(self.config_file):
            with open(self.config_file, 'r') as f:
                loaded_config = json.load(f)
                # Deep merge with default to ensure new keys are present
                default_config['mikrotik'].update(loaded_config.get('mikrotik', {}))
                default_config['server'].update(loaded_config.get('server', {}))
                return default_config
        else:
            with open(self.config_file, 'w') as f:
                json.dump(default_config, f, indent=4)
            return default_config

    def get_config(self):
        return self.config

    def update_config(self, new_config):
        self.config['mikrotik'].update(new_config.get('mikrotik', {}))
        self.config['server'].update(new_config.get('server', {}))
        with open(self.config_file, 'w') as f:
            json.dump(self.config, f, indent=4)

    def reset_mikrotik_config_to_defaults(self):
        """Resets the Mikrotik part of the configuration to its original defaults."""
        # Get the original default settings as defined in _load_config
        # This is a bit indirect; _load_config itself returns a merged config.
        # For true defaults, we define it here or access a pristine default structure.
        # Let's re-fetch default structure as defined in _load_config's initial state.
        original_default_settings = { # Replicating the default structure from _load_config
            "host": "192.168.88.1",
            "port": 8728,
            "username": "admin",
            "password": "",
            "use_ssl": False,
            "hotspot_login_url": "http://hotspot.setup/login"
        }
        # The server part of the config should remain untouched by this.
        current_server_config = self.config.get('server', {})

        update_payload = {
            'mikrotik': original_default_settings,
            'server': current_server_config # Ensure server settings are preserved
        }
        # Instead of self.update_config which merges, we want to overwrite mikrotik section
        # and keep server section. So, construct the full new config.
        self.config['mikrotik'] = original_default_settings
        # self.config['server'] is already what it should be.

        with open(self.config_file, 'w') as f:
            json.dump(self.config, f, indent=4)
        logger.info("Mikrotik configuration has been reset to defaults.")


# Initialize ConfigLoader
config_loader = ConfigLoader()
app_config = config_loader.get_config()

# Define exempt endpoints that do not require a Mikrotik connection
EXEMPT_ENDPOINTS = {'login_page', 'initial_connect', 'static'} # 'static' is Flask's default for static files

@app.before_request
def require_mikrotik_connection():
    logger.debug(f"before_request: endpoint='{request.endpoint}', path='{request.path}'")
    # If the requested endpoint is exempt, do nothing.
    if request.endpoint in EXEMPT_ENDPOINTS:
        logger.debug(f"before_request: Endpoint '{request.endpoint}' is exempt. Allowing request.")
        return

    # For specific file requests that might not have typical endpoints (e.g. favicon.ico)
    # This is a bit of a catch-all; ideally, static assets are handled by 'static' endpoint.
    # This check should ideally be more specific or rely on Flask's static handling.
    if '.' in request.path and not request.endpoint: # request.endpoint might be None for unhandled paths
        logger.debug(f"before_request: Path '{request.path}' appears to be a file request and has no specific endpoint. Allowing.")
        return

    logger.debug(f"before_request: Endpoint '{request.endpoint}' requires Mikrotik connection check.")
    # Try to establish a connection. get_mikrotik_api will return None on failure.
    api = get_mikrotik_api()
    if api is None:
        logger.warning(f"No active Mikrotik connection for endpoint '{request.endpoint}'. API is None. Redirecting to login.")
        # Using url_for with the function name of the route
        return redirect(url_for('login_page'))
        # The 'login_page' is the function name for the @app.route('/') route.
    else:
        logger.debug(f"before_request: Mikrotik API obtained for endpoint '{request.endpoint}'. Allowing request.")
        # Explicitly return None, which means the request is allowed to proceed.
        # Not returning anything (implicit None) is the standard way.
        return


@app.route('/api/logout', methods=['POST'])
def logout():
    global app_config
    logger.info("Processing logout request.")
    config_loader.reset_mikrotik_config_to_defaults()
    app_config = config_loader.get_config() # Reload global app_config to reflect reset state

    # Optionally, clear the connection from 'g' if it exists, though
    # it will be cleared on next request context anyway.
    if 'mikrotik_api' in g:
        g.pop('mikrotik_api', None)
    if 'mikrotik_connection' in g:
        # Attempt to close if it's a real connection object
        conn_to_close = g.pop('mikrotik_connection', None)
        if conn_to_close and hasattr(conn_to_close, 'close'):
            try:
                conn_to_close.close()
                logger.info("Closed active Mikrotik connection from 'g' during logout.")
            except Exception as e:
                logger.error(f"Error closing connection from 'g' during logout: {e}")

    logger.info("User logged out, Mikrotik configuration reset to defaults.")
    return jsonify({'success': True, 'message': 'Logged out successfully.'})

def get_mikrotik_api():
    """Establishes and returns a single Mikrotik API connection per request."""
    logger.debug(f"get_mikrotik_api: Current Mikrotik config host from module-level app_config: {app_config['mikrotik'].get('host')}")
    if 'mikrotik_api' not in g:
        logger.debug("get_mikrotik_api: 'mikrotik_api' not in g. Attempting new connection.")
        # Fetch the latest config directly from the loader instance for new connections
        current_loaded_config = config_loader.get_config()
        mikrotik_config = current_loaded_config['mikrotik']
        logger.debug(f"get_mikrotik_api: Using host from config_loader.get_config(): {mikrotik_config.get('host')}")

        host, port, username, password, use_ssl = (
            mikrotik_config['host'], mikrotik_config['port'],
            mikrotik_config['username'], mikrotik_config['password'],
            mikrotik_config.get('use_ssl', False)
        )
        # Basic check for placeholder/default config before attempting connection
        if host == "192.168.88.1" and username == "admin" and password == "" and not os.path.exists(config_loader.config_file):
             logger.warning("get_mikrotik_api: Attempting to connect with default placeholder config and no config file saved yet. Connection will likely fail or use defaults.")

        logger.info(f"Attempting to connect to Mikrotik: {host}:{port} (SSL: {use_ssl})")
        try:
            g.mikrotik_connection = librouteros.connect(
                host=host, username=username, password=password, port=port, ssl=use_ssl
            )
            g.mikrotik_api = g.mikrotik_connection
            logger.info("Mikrotik connection established in get_mikrotik_api.")
        except (librouteros.exceptions.LibRouterosError, TrapError, socket.error, ConnectionRefusedError, OSError) as e: # More specific exceptions
            logger.error(f"Mikrotik connection failed in get_mikrotik_api: {e}")
            g.mikrotik_api = None # Ensure it's None if connection fails
        except Exception as e: # Catch any other unexpected error during connection
            logger.error(f"Unexpected error during Mikrotik connection in get_mikrotik_api: {e}")
            g.mikrotik_api = None # Ensure it's None
    else:
        logger.debug("get_mikrotik_api: Reusing existing Mikrotik API from 'g'.")

    api_to_return = g.get('mikrotik_api', None)
    logger.debug(f"get_mikrotik_api: Returning API object: {'Exists' if api_to_return else 'None'}")
    return api_to_return

@app.teardown_appcontext
def teardown_connection(exception):
    """Closes the Mikrotik connection after each request."""
    mikrotik_connection = g.pop('mikrotik_connection', None)
    if mikrotik_connection:
        mikrotik_connection.close()
        logger.info("Mikrotik connection closed.")


class RouterOSService:
    """Service class for all Mikrotik RouterOS interactions."""
    def __init__(self):
        pass # Connection is managed globally via get_mikrotik_api

    def test_connection(self) -> tuple[bool, str]:
        """Test connection to Mikrotik router."""
        try:
            api = get_mikrotik_api()
            if api is None:
                return False, "Connection failed: Could not establish API session. Check config and router status."
            identity_records = list(api.path('system', 'identity').select('name'))

            router_name = 'Mikrotik Router'
            if identity_records:
                router_name = identity_records[0].get('name', 'Mikrotik Router')

            return True, f"Connected successfully to {router_name}"
        except ConnectionError as e:
            logger.error(f"Connection test failed: {e}")
            return False, f"Connection failed: {e}"
        except Exception as e:
            logger.error(f"Unexpected error during connection test: {str(e)}")
            return False, f"Unexpected error during connection test: {str(e)}"

    def get_hotspot_users(self) -> list:
        """Get all hotspot users."""
        try:
            api = get_mikrotik_api()
            if api is None:
                logger.error("Error getting users: Mikrotik API not available.")
                return []
            users = list(api.path('ip', 'hotspot', 'user').select(
                '.id', 'name', 'password', 'profile', 'disabled', 'limit-uptime', 'limit-bytes-total',
                'uptime', 'bytes-in', 'bytes-out', 'comment', 'limit-bytes-in', 'limit-bytes-out'
            ))
            return users
        except Exception as e:
            logger.error(f"Error getting users: {str(e)}")
            return []

    def create_hotspot_user(self, user_data: dict) -> tuple[bool, str]:
        """Create new hotspot user."""
        try:
            api = get_mikrotik_api()
            if api is None:
                return False, "Mikrotik connection not available"
            # Clean up potential None values before sending to router
            valid_user_data = {k: v for k, v in user_data.items() if v is not None}
            api.path('ip', 'hotspot', 'user').add(**valid_user_data)
            return True, "User created successfully"
        except (TrapError, Exception) as e:
            logger.error(f"Error creating user: {str(e)}")
            return False, f"Mikrotik Error: {str(e)}"

    def edit_hotspot_user(self, username: str, new_data: dict) -> tuple[bool, str]:
        """Edit existing hotspot user."""
        try:
            api = get_mikrotik_api()
            if api is None:
                return False, "Mikrotik connection not available"
            users = list(api.path('ip', 'hotspot', 'user').select('.id').where(name=username))
            if not users:
                return False, "User not found"
            
            user_id = users[0]['.id']
            api.path('ip', 'hotspot', 'user').set(**new_data, **{'.id': user_id})
            return True, "User updated successfully"
        except (TrapError, Exception) as e:
            logger.error(f"Error editing user '{username}': {str(e)}")
            return False, f"Mikrotik Error: {str(e)}"

    def delete_hotspot_user(self, username: str) -> tuple[bool, str]:
        """Delete hotspot user."""
        try:
            api = get_mikrotik_api()
            if api is None:
                return False, "Mikrotik connection not available"
            users = list(api.path('ip', 'hotspot', 'user').select('.id').where(name=username))
            if not users:
                return False, "User not found"

            user_id = users[0]['.id']
            api.path('ip', 'hotspot', 'user').remove(user_id)
            return True, "User deleted successfully"
        except (TrapError, Exception) as e:
            logger.error(f"Error deleting user '{username}': {str(e)}")
            return False, f"Mikrotik Error: {str(e)}"
    
    def get_active_sessions(self) -> list:
        """Get active hotspot sessions."""
        try:
            api = get_mikrotik_api()
            if api is None:
                logger.error("Error getting active sessions: Mikrotik API not available.")
                return []
            sessions = list(api.path('ip', 'hotspot', 'active').select(
                'user', 'address', 'mac-address', 'uptime', 'bytes-in', 'bytes-out',
                'session-time-left', 'idle-time', '.id'
            ))
            return sessions
        except Exception as e:
            logger.error(f"Error getting active sessions: {str(e)}")
            return []

    def disconnect_user(self, active_id: str) -> tuple[bool, str]:
        """Disconnect active user session by its .id."""
        try:
            api = get_mikrotik_api()
            if api is None:
                return False, "Mikrotik connection not available"
            api.path('ip', 'hotspot', 'active').remove(active_id)
            return True, "User disconnected successfully"
        except (TrapError, Exception) as e:
            logger.error(f"Error disconnecting user: {str(e)}")
            return False, f"Mikrotik Error: {str(e)}"

    def get_user_profiles(self) -> list:
        """Get hotspot user profiles."""
        try:
            api = get_mikrotik_api()
            if api is None:
                logger.error("Error getting profiles: Mikrotik API not available.")
                return []
            profiles = list(api.path('ip', 'hotspot', 'user', 'profile').select(
                '.id', 'name', 'rate-limit', 'session-timeout', 'shared-users',
                'mac-cookie-timeout', 'keepalive-timeout'
            ))
            return profiles
        except Exception as e:
            logger.error(f"Error getting profiles: {str(e)}")
            return []
    
    def create_hotspot_profile(self, profile_data: dict) -> tuple[bool, str]:
        """Creates a new hotspot user profile."""
        try:
            api = get_mikrotik_api()
            if api is None:
                return False, "Mikrotik connection not available"
            data_to_add = {k: v for k, v in profile_data.items() if v}
            api.path('ip', 'hotspot', 'user', 'profile').add(**data_to_add)
            return True, f"Profile '{profile_data['name']}' created successfully."
        except (TrapError, Exception) as e:
            logger.error(f"Error creating profile: {str(e)}")
            return False, f"Mikrotik Error: {str(e)}"

    def edit_hotspot_profile(self, profile_id: str, new_data: dict) -> tuple[bool, str]:
        """Edits an existing hotspot user profile."""
        try:
            api = get_mikrotik_api()
            if api is None:
                return False, "Mikrotik connection not available"
            api.path('ip', 'hotspot', 'user', 'profile').set(**new_data, **{'.id': profile_id})
            return True, "Profile updated successfully."
        except (TrapError, Exception) as e:
            logger.error(f"Error editing profile: {str(e)}")
            return False, f"Mikrotik Error: {str(e)}"

    def delete_hotspot_profile(self, profile_id: str) -> tuple[bool, str]:
        """Deletes a hotspot user profile."""
        try:
            api = get_mikrotik_api()
            if api is None:
                return False, "Mikrotik connection not available"
            api.path('ip', 'hotspot', 'user', 'profile').remove(profile_id)
            return True, "Profile deleted successfully."
        except (TrapError, Exception) as e:
            logger.error(f"Error deleting profile: {str(e)}")
            return False, f"Mikrotik Error: {str(e)}"
    
    def _parse_ros_time(self, time_str: str) -> int:
        """Parses RouterOS time string (e.g., 1w2d3h4m5s) into seconds."""
        if not time_str:
            return 0
        total_seconds = 0
        matches = re.findall(r'(\d+)([wdhms])', time_str)
        for value, unit in matches:
            value = int(value)
            if unit == 'w': total_seconds += value * 604800
            elif unit == 'd': total_seconds += value * 86400
            elif unit == 'h': total_seconds += value * 3600
            elif unit == 'm': total_seconds += value * 60
            elif unit == 's': total_seconds += value
        return total_seconds

    def find_and_delete_expired_users(self) -> tuple[bool, str, int]:
        """Finds and deletes users who have exceeded their time or data limits."""
        try:
            api = get_mikrotik_api()
            if api is None: # Check if API connection failed initially
                return False, "Mikrotik connection not available", 0

            users = self.get_hotspot_users()
            # get_hotspot_users itself will return [] if api was None, so this is safe.
            # However, if api was None for this call but not for the initial api check,
            # we might want to re-check. But the current pattern is one api per request.
            if not users and api is None: # If users list is empty because api became None
                 return False, "Mikrotik connection not available (users fetch failed)", 0

            deleted_count = 0
            errors = []

            for user in users:
                is_expired = False
                # Check time limit
                if user.get('limit-uptime') and user['limit-uptime'] != '0s':
                    limit_sec = self._parse_ros_time(user['limit-uptime'])
                    usage_sec = self._parse_ros_time(user.get('uptime', '0s'))
                    if limit_sec > 0 and usage_sec >= limit_sec:
                        is_expired = True

                # Check total data limit
                if not is_expired and user.get('limit-bytes-total') and int(user['limit-bytes-total']) > 0:
                    limit_bytes = int(user['limit-bytes-total'])
                    usage_bytes = int(user.get('bytes-in', 0)) + int(user.get('bytes-out', 0))
                    if usage_bytes >= limit_bytes:
                        is_expired = True
                
                if is_expired:
                    try:
                        api.path('ip', 'hotspot', 'user').remove(user['.id'])
                        deleted_count += 1
                        logger.info(f"Deleted expired user '{user['name']}'")
                    except Exception as e:
                        errors.append(user['name'])
                        logger.error(f"Failed to delete expired user '{user['name']}': {e}")
            
            message = f"Successfully deleted {deleted_count} expired user(s)."
            if errors:
                message += f" Failed to delete: {', '.join(errors)}."
            
            return True, message, deleted_count
        except Exception as e:
            logger.error(f"Error during expired user cleanup: {str(e)}")
            return False, f"An unexpected error occurred: {str(e)}", 0

router_os_service = RouterOSService()

# --- Helper Functions ---
def format_bytes_for_export(bytes_val):
    if not bytes_val or bytes_val == '0' or bytes_val == '': return "Unlimited"
    try:
        numeric_bytes = float(bytes_val)
        if numeric_bytes == 0: return "Unlimited"
        k = 1024
        sizes = ['B', 'KB', 'MB', 'GB', 'TB']
        if numeric_bytes < 1: return f"{numeric_bytes:.0f} B"
        i = min(len(sizes) - 1, int(math.floor(math.log(numeric_bytes) / math.log(k))))
        return f"{numeric_bytes / math.pow(k, i):.2f} {sizes[i]}"
    except (ValueError, TypeError):
        return str(bytes_val)

def generate_qr_code_base64(login_url, username, password):
    """Generates a QR code for login and returns a Base64 encoded string."""
    if not QRCODE_AVAILABLE: return None
    
    full_url = f"{login_url}?username={username}&password={password}"
    qr = qrcode.QRCode(version=1, box_size=10, border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(full_url)
    qr.make(fit=True)
    
    img = qr.make_image(image_factory=StyledPilImage, module_drawer=RoundedModuleDrawer())
    
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode('utf-8')

# --- Flask Routes ---
@app.route('/')
def login_page():
    """Serves the login page."""
    return send_from_directory(get_base_path(), 'login.html')

@app.route('/dashboard')
def index():
    """Serves the main dashboard page."""
    # TODO: Add authentication check here in a later step
    return send_from_directory(get_base_path(), 'mikrotik_userman_dashboard.html')

@app.route('/api/initial-connect', methods=['POST'])
def initial_connect():
    global app_config # Ensure we're updating the global app_config
    data = request.json
    host = data.get('host')
    port = data.get('port')
    username = data.get('username')
    password = data.get('password') # Password can be empty

    if not all([host, port is not None, username is not None]): # port can be 0, username can be empty string
        return jsonify({'success': False, 'message': 'Host, Port, and Username are required.'}), 400

    try:
        port = int(port)
        if not (0 <= port <= 65535): # Port 0 is technically valid for OS to pick one
            raise ValueError("Invalid port number")
    except ValueError:
        return jsonify({'success': False, 'message': 'Invalid port number. Must be between 0 and 65535.'}), 400

    logger.info(f"Attempting initial connection to Mikrotik: {host}:{port} with user: {username}")
    try:
        # Attempt connection
        temp_conn = librouteros.connect(
            host=host,
            username=username,
            password=password,
            port=port,
            ssl=app_config['mikrotik'].get('use_ssl', False) # Use current SSL setting or default
        )
        temp_conn.close() # Close if successful, we just tested it.
        logger.info("Initial connection test successful.")

        # Update config.json
        new_mikrotik_config = {
            "host": host,
            "port": port,
            "username": username,
            "password": password, # Save the password
            "use_ssl": app_config['mikrotik'].get('use_ssl', False), # Preserve existing SSL setting
            "hotspot_login_url": app_config['mikrotik'].get('hotspot_login_url', '') # Preserve existing
        }
        config_loader.update_config({'mikrotik': new_mikrotik_config})
        app_config = config_loader.get_config() # Reload app_config to reflect changes

        return jsonify({'success': True, 'message': 'Successfully connected and configuration saved.'})

    except (librouteros.exceptions.LibRouterosError, TrapError, socket.error, ConnectionRefusedError, OSError) as e:
        logger.error(f"Initial connection failed: {e}")
        # Sanitize error message for user
        error_message = str(e)
        if "authentication failed" in error_message.lower():
            return jsonify({'success': False, 'message': 'Authentication failed. Please check username and password.'}), 401
        elif "connection refused" in error_message.lower() or "timed out" in error_message.lower() or "no route to host" in error_message.lower():
            return jsonify({'success': False, 'message': 'Connection refused or timed out. Check IP address, port, and router firewall.'}), 400
        return jsonify({'success': False, 'message': f'Connection failed: {e}.'}), 400
    except Exception as e:
        logger.error(f"Unexpected error during initial connection: {e}")
        return jsonify({'success': False, 'message': f'An unexpected error occurred: {e}.'}), 500


@app.route('/api/test-connection', methods=['POST'])
def test_connection():
    success, message = router_os_service.test_connection()
    return jsonify({'success': success, 'message': message})

@app.route('/api/config', methods=['GET'])
def get_config_route():
    cfg = config_loader.get_config()
    # Add status of optional features
    cfg['features'] = {
        'pdf_export': WEASYPRINT_AVAILABLE,
        'qr_codes': QRCODE_AVAILABLE
    }
    return jsonify(cfg)

@app.route('/api/config', methods=['POST'])
def update_config_route():
    data = request.json
    config_loader.update_config(data)
    return jsonify({'success': True, 'message': 'Configuration updated and saved.'})

@app.route('/api/dashboard-stats', methods=['GET'])
def get_dashboard_stats():
    users = router_os_service.get_hotspot_users()
    sessions = router_os_service.get_active_sessions()
    total_users = len(users)
    active_sessions = len(sessions)
    return jsonify({'total_users': total_users, 'active_sessions': active_sessions})

@app.route('/api/users', methods=['GET'])
def get_users():
    users = router_os_service.get_hotspot_users()
    return jsonify({'users': users})

@app.route('/api/users', methods=['POST'])
def create_user():
    data = request.json
    username = data.get('name')
    password = data.get('password')
    if not username or not password:
        return jsonify({'success': False, 'message': 'Username and password are required.'}), 400
    
    success, message = router_os_service.create_hotspot_user(data)
    return jsonify({'success': success, 'message': message})
    
@app.route('/api/bulk-create-users', methods=['POST'])
def bulk_create_users():
    data = request.json
    number_of_users = data.get('number_of_users')
    profile = data.get('profile')
    username_length = int(data.get('username_length', 6))
    password_length = int(data.get('password_length', 8))

    if not all([number_of_users, profile]):
        return jsonify({'success': False, 'message': 'Number of users and profile are required.'}), 400

    base_user_data = {k: v for k, v in data.items() if k not in ['number_of_users', 'username_length', 'password_length'] and v}

    created_credentials = []
    errors = []
    
    for _ in range(int(number_of_users)):
        username = ''.join(random.choices(string.ascii_letters + string.digits, k=username_length))
        password = ''.join(random.choices(string.ascii_letters + string.digits, k=password_length))
        
        user_data = base_user_data.copy()
        user_data['name'] = username
        user_data['password'] = password
        if data.get('comment_prefix'):
            user_data['comment'] = f"{data['comment_prefix']}{username}"

        success, msg = router_os_service.create_hotspot_user(user_data)
        if success:
            created_credentials.append({'username': username, 'password': password})
        else:
            errors.append({'username': username, 'error': msg})

    return jsonify({
        'success': len(errors) == 0,
        'message': f"Created {len(created_credentials)} users. Failed: {len(errors)}.",
        'created_credentials': created_credentials,
        'errors': errors
    })

@app.route('/api/users/<username>', methods=['PUT'])
def edit_user(username: str):
    data = request.json
    if 'disabled' in data:
        data['disabled'] = 'true' if data['disabled'] else 'false'

    success, message = router_os_service.edit_hotspot_user(username, data)
    return jsonify({'success': success, 'message': message})

@app.route('/api/users/<username>', methods=['DELETE'])
def delete_user(username: str):
    success, message = router_os_service.delete_hotspot_user(username)
    return jsonify({'success': success, 'message': message})

@app.route('/api/active-sessions', methods=['GET'])
def get_active_sessions_route():
    sessions = router_os_service.get_active_sessions()
    return jsonify({'sessions': sessions})

@app.route('/api/disconnect-user/<active_id>', methods=['POST'])
def disconnect_user_session(active_id: str):
    success, message = router_os_service.disconnect_user(active_id)
    return jsonify({'success': success, 'message': message})

@app.route('/api/delete-expired-users', methods=['POST'])
def delete_expired_users_route():
    success, message, count = router_os_service.find_and_delete_expired_users()
    return jsonify({'success': success, 'message': message, 'deleted_count': count})

# --- Profile Management Routes ---
@app.route('/api/profiles', methods=['GET'])
def get_profiles_route():
    profiles = router_os_service.get_user_profiles()
    return jsonify({'profiles': profiles})

@app.route('/api/profiles', methods=['POST'])
def create_profile_route():
    data = request.json
    if not data.get('name'):
        return jsonify({'success': False, 'message': 'Profile name is required.'}), 400
    success, message = router_os_service.create_hotspot_profile(data)
    return jsonify({'success': success, 'message': message})

@app.route('/api/profiles/<profile_id>', methods=['PUT'])
def edit_profile_route(profile_id: str):
    data = request.json
    if not data:
        return jsonify({'success': False, 'message': 'No data provided for update.'}), 400
    success, message = router_os_service.edit_hotspot_profile(profile_id, data)
    return jsonify({'success': success, 'message': message})

@app.route('/api/profiles/<profile_id>', methods=['DELETE'])
def delete_profile_route(profile_id: str):
    success, message = router_os_service.delete_hotspot_profile(profile_id)
    return jsonify({'success': success, 'message': message})

# --- MODIFIED EXPORT ROUTE ---
@app.route('/api/export-users', methods=['GET'])
def export_users_route():
    export_format = request.args.get('format', 'json').lower()
    profile_filter = request.args.get('profile_filter')

    users = router_os_service.get_hotspot_users()

    if profile_filter:
        users = [user for user in users if user.get('profile') == profile_filter]

    if not users:
        return "No users found for the selected criteria.", 404

    if export_format == 'json':
        return jsonify(users)

    elif export_format == 'csv':
        si = io.StringIO()
        cw = csv.writer(si)
        headers = [
            'Username', 'Password', 'Profile', 'Time Limit',
            'Total Data Limit', 'Comment', 'Disabled'
        ]
        cw.writerow(headers)
        for user in users:
            row = [
                user.get('name', ''), user.get('password', ''), user.get('profile', ''),
                user.get('limit-uptime', 'Unlimited'),
                format_bytes_for_export(user.get('limit-bytes-total')),
                user.get('comment', ''), user.get('disabled', 'false')
            ]
            cw.writerow(row)
        output = si.getvalue()
        return Response(output, mimetype="text/csv", headers={"Content-disposition": f"attachment; filename=users_{profile_filter or 'all'}.csv"})

    elif export_format == 'html_voucher' or export_format == 'pdf_voucher':
        login_url = app_config['mikrotik'].get('hotspot_login_url', '')
        if not login_url:
            logger.warning("hotspot_login_url not set in config.json. QR Codes will not work.")

        html_content = """
        <html><head><title>Hotspot Vouchers</title>
        <style>
            body { font-family: 'Segoe UI', sans-serif; margin: 10px; background-color: #f4f4f9; }
            .voucher-container { display: grid; grid-template-columns: repeat(auto-fit, minmax(350px, 1fr)); gap: 10px; }
            .voucher { background: white; border: 1px solid #ddd; border-radius: 12px; padding: 15px; page-break-inside: avoid; display: flex; box-shadow: 0 2px 5px rgba(0,0,0,0.05); }
            .voucher-details { flex-grow: 1; }
            .voucher-qr { flex-shrink: 0; width: 120px; height: 120px; margin-left: 15px; }
            .voucher-header { text-align: center; border-bottom: 2px dashed #6a11cb; margin-bottom: 10px; padding-bottom: 5px; }
            .voucher-header h3 { margin: 0; font-size: 1.2em; color: #2575fc; }
            .credentials p { font-size: 1.1em; margin: 8px 0; }
            .credentials strong { color: #333; }
            .credentials span { font-family: 'Courier New', monospace; background: #eee; padding: 3px 6px; border-radius: 4px; color: #d63384; font-weight: bold; }
            .voucher-info { font-size: 0.8em; color: #555; margin-top: 10px; border-top: 1px solid #eee; padding-top: 8px; }
            @media print { body { margin: 0; background: #fff; } .voucher { box-shadow: none; border: 1px dashed #999; } }
        </style></head><body><div class="voucher-container">
        """
        for user in users:
            qr_code_b64 = generate_qr_code_base64(login_url, user.get('name'), user.get('password')) if login_url else None
            html_content += f"""
            <div class="voucher">
                <div class="voucher-details">
                    <div class="voucher-header"><h3>Hotspot Access</h3></div>
                    <div class="credentials">
                        <p><strong>Username:</strong> <span>{user.get('name', 'N/A')}</span></p>
                        <p><strong>Password:</strong> <span>{user.get('password') or 'N/A'}</span></p>
                    </div>
                    <div class="voucher-info">
                        <strong>Profile:</strong> {user.get('profile', 'N/A')} | 
                        <strong>Time Limit:</strong> {user.get('limit-uptime') or 'Unlimited'} | 
                        <strong>Data Limit:</strong> {format_bytes_for_export(user.get('limit-bytes-total'))}
                    </div>
                </div>
                """
            if qr_code_b64:
                html_content += f'<div class="voucher-qr"><img src="data:image/png;base64,{qr_code_b64}" style="width:100%;height:100%;"></div>'
            html_content += "</div>"
        html_content += "</div></body></html>"

        if export_format == 'pdf_voucher':
            if not WEASYPRINT_AVAILABLE:
                return jsonify({"success": False, "message": "PDF generation is disabled. Please install system dependencies for WeasyPrint and restart the application."}), 501
            try:
                pdf_file = WeasyHTML(string=html_content).write_pdf()
                return Response(pdf_file, mimetype="application/pdf", headers={"Content-disposition": f"attachment; filename=vouchers_{profile_filter or 'all'}.pdf"})
            except Exception as e:
                 logger.error(f"Failed to generate PDF: {e}")
                 return jsonify({"success": False, "message": f"An unexpected error occurred during PDF generation: {e}"}), 500
        else: # html_voucher
            return Response(html_content, mimetype="text/html")

    else:
        return jsonify({"success": False, "message": "Invalid export format."}), 400


if __name__ == '__main__':
    server_config = app_config['server']
    print("="*40)
    print("  Mikrotik Hotspot Management System v2")
    print("="*40)
    print(f"\n✅ Dashboard available at: http://{server_config['host']}:{server_config['port']}")
    app.run(host=server_config['host'], port=server_config['port'], debug=server_config['debug'])
