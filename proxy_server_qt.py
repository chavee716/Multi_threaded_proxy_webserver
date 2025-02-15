import sys
import ssl
import socket
import threading
import logging
import queue
from datetime import datetime
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                           QHBoxLayout, QLabel, QLineEdit, QPushButton, 
                           QTextEdit, QGroupBox, QLCDNumber, QTabWidget,
                           QTableWidget, QTableWidgetItem, QHeaderView)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread

class ProxyWorker(QThread):
    """Worker thread for proxy server"""
    connectionUpdate = pyqtSignal(int, int)  # active_connections, total_requests
    logUpdate = pyqtSignal(str)
    connectionAdded = pyqtSignal(list)  # [timestamp, client_ip, target_host, status, protocol]

    def __init__(self, host, port):
        super().__init__()
        self.host = host
        self.port = port
        self.running = False
        self.active_connections = 0
        self.total_requests = 0
        self.BUFFER_SIZE = 8192

    def run(self):
        """Main server loop"""
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        try:
            server_socket.bind((self.host, self.port))
            server_socket.listen(10)
            self.running = True
            self.logUpdate.emit(f"Server started on {self.host}:{self.port}")

            while self.running:
                try:
                    client_socket, client_address = server_socket.accept()
                    self.active_connections += 1
                    self.total_requests += 1
                    self.connectionUpdate.emit(self.active_connections, self.total_requests)
                    
                    thread = threading.Thread(
                        target=self.handle_client,
                        args=(client_socket, client_address)
                    )
                    thread.daemon = True
                    thread.start()
                    
                except Exception as e:
                    if self.running:
                        self.logUpdate.emit(f"Error accepting connection: {e}")
                    
        except Exception as e:
            self.logUpdate.emit(f"Server error: {e}")
        finally:
            server_socket.close()

    def handle_client(self, client_socket, client_address):
        """Handle client connection"""
        try:
            # Receive initial request
            request_data = client_socket.recv(self.BUFFER_SIZE)
            if not request_data:
                return

            # Decode the first line to determine if it's CONNECT (HTTPS) or not
            first_line = request_data.decode('utf-8', errors='ignore').split('\n')[0]
            method = first_line.split(' ')[0]

            if method == 'CONNECT':
                self.handle_https_tunnel(client_socket, client_address, first_line)
            else:
                self.handle_http_request(client_socket, client_address, request_data)

        except Exception as e:
            self.logUpdate.emit(f"Error handling client {client_address}: {e}")
        finally:
            self.active_connections -= 1
            self.connectionUpdate.emit(self.active_connections, self.total_requests)

    def handle_https_tunnel(self, client_socket, client_address, first_line):
        """Handle HTTPS tunneling"""
        try:
            # Extract target host and port from CONNECT request
            target = first_line.split(' ')[1]
            host, port = target.split(':')
            port = int(port)

            # Add connection to table
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.connectionAdded.emit([
                timestamp,
                client_address[0],
                host,
                "CONNECT",
                "HTTPS"
            ])

            # Create connection to target server
            server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                server_socket.connect((host, port))
                # Send 200 Connection established to the client
                response = "HTTP/1.1 200 Connection established\r\n\r\n"
                client_socket.send(response.encode())
            except Exception as e:
                self.logUpdate.emit(f"Failed to connect to {host}:{port} - {e}")
                return

            # Start bidirectional tunneling
            self.tunnel_traffic(client_socket, server_socket)

        except Exception as e:
            self.logUpdate.emit(f"HTTPS tunneling error: {e}")
        finally:
            client_socket.close()

    def tunnel_traffic(self, client_socket, server_socket):
        """Handle bidirectional tunneling between client and server"""
        def forward(source, destination, direction):
            try:
                while True:
                    data = source.recv(self.BUFFER_SIZE)
                    if not data:
                        break
                    destination.send(data)
            except Exception as e:
                self.logUpdate.emit(f"Tunneling error ({direction}): {e}")

        # Create threads for bidirectional forwarding
        client_to_server = threading.Thread(
            target=forward,
            args=(client_socket, server_socket, "client → server")
        )
        server_to_client = threading.Thread(
            target=forward,
            args=(server_socket, client_socket, "server → client")
        )

        client_to_server.daemon = True
        server_to_client.daemon = True

        # Start forwarding threads
        client_to_server.start()
        server_to_client.start()

        # Wait for both directions to complete
        client_to_server.join()
        server_to_client.join()

        # Clean up sockets
        server_socket.close()
        client_socket.close()

    def handle_http_request(self, client_socket, client_address, request_data):
        """Handle regular HTTP request"""
        try:
            # Parse request
            first_line = request_data.decode('utf-8').split('\n')[0]
            url = first_line.split(' ')[1]
            
            # Extract target host
            http_pos = url.find("://")
            if http_pos == -1:
                temp = url
            else:
                temp = url[(http_pos + 3):]

            port_pos = temp.find(":")
            webserver_pos = temp.find("/")
            if webserver_pos == -1:
                webserver_pos = len(temp)

            webserver = ""
            port = -1
            if port_pos == -1 or webserver_pos < port_pos:
                port = 80
                webserver = temp[:webserver_pos]
            else:
                port = int(temp[(port_pos + 1):][:webserver_pos - port_pos - 1])
                webserver = temp[:port_pos]

            # Add connection to table
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.connectionAdded.emit([
                timestamp,
                client_address[0],
                webserver,
                "Connected",
                "HTTP"
            ])

            # Connect to target server
            proxy_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            proxy_socket.connect((webserver, port))
            proxy_socket.send(request_data)

            # Forward data
            while True:
                response = proxy_socket.recv(self.BUFFER_SIZE)
                if len(response) > 0:
                    client_socket.send(response)
                else:
                    break

            proxy_socket.close()
            client_socket.close()

        except Exception as e:
            self.logUpdate.emit(f"HTTP handling error: {e}")

    def stop(self):
        """Stop the proxy server"""
        self.running = False

class ProxyServerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Modern Proxy Server (HTTP/HTTPS)")
        self.setMinimumSize(1000, 700)  # Increased size for better visibility
        self.proxy_worker = None
        self.initUI()

    def initUI(self):
        """Initialize the user interface"""
        # Create central widget and main layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # Create server controls group
        controls_group = QGroupBox("Server Controls")
        controls_layout = QHBoxLayout()
        controls_group.setLayout(controls_layout)

        # Host input
        host_label = QLabel("Host:")
        self.host_input = QLineEdit("127.0.0.1")
        controls_layout.addWidget(host_label)
        controls_layout.addWidget(self.host_input)

        # Port input
        port_label = QLabel("Port:")
        self.port_input = QLineEdit("8080")
        controls_layout.addWidget(port_label)
        controls_layout.addWidget(self.port_input)

        # Start/Stop button
        self.toggle_button = QPushButton("Start Server")
        self.toggle_button.clicked.connect(self.toggle_server)
        controls_layout.addWidget(self.toggle_button)

        layout.addWidget(controls_group)

        # Create statistics group
        stats_group = QGroupBox("Statistics")
        stats_layout = QHBoxLayout()
        stats_group.setLayout(stats_layout)

        # Active connections display
        self.active_connections_lcd = QLCDNumber()
        self.active_connections_lcd.setSegmentStyle(QLCDNumber.SegmentStyle.Filled)
        stats_layout.addWidget(QLabel("Active Connections:"))
        stats_layout.addWidget(self.active_connections_lcd)

        # Total requests display
        self.total_requests_lcd = QLCDNumber()
        self.total_requests_lcd.setSegmentStyle(QLCDNumber.SegmentStyle.Filled)
        stats_layout.addWidget(QLabel("Total Requests:"))
        stats_layout.addWidget(self.total_requests_lcd)

        layout.addWidget(stats_group)

        # Create tab widget for logs and connections
        tab_widget = QTabWidget()
        
        # Logs tab
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        tab_widget.addTab(self.log_text, "Logs")

        # Connections tab
        self.connections_table = QTableWidget()
        self.connections_table.setColumnCount(5)  # Added Protocol column
        self.connections_table.setHorizontalHeaderLabels([
            "Timestamp", "Client IP", "Target Host", "Status", "Protocol"
        ])
        header = self.connections_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        tab_widget.addTab(self.connections_table, "Connections")

        layout.addWidget(tab_widget)

        # Clear buttons
        buttons_layout = QHBoxLayout()
        clear_logs_button = QPushButton("Clear Logs")
        clear_logs_button.clicked.connect(self.clear_logs)
        clear_connections_button = QPushButton("Clear Connections")
        clear_connections_button.clicked.connect(self.clear_connections)
        buttons_layout.addWidget(clear_logs_button)
        buttons_layout.addWidget(clear_connections_button)
        layout.addLayout(buttons_layout)

    def toggle_server(self):
        """Start or stop the proxy server"""
        if not self.proxy_worker:
            try:
                host = self.host_input.text()
                port = int(self.port_input.text())
                
                self.proxy_worker = ProxyWorker(host, port)
                self.proxy_worker.connectionUpdate.connect(self.update_stats)
                self.proxy_worker.logUpdate.connect(self.add_log)
                self.proxy_worker.connectionAdded.connect(self.add_connection)
                self.proxy_worker.start()
                
                self.toggle_button.setText("Stop Server")
                self.host_input.setEnabled(False)
                self.port_input.setEnabled(False)
                
            except Exception as e:
                self.add_log(f"Failed to start server: {e}")
        else:
            self.proxy_worker.stop()
            self.proxy_worker = None
            self.toggle_button.setText("Start Server")
            self.host_input.setEnabled(True)
            self.port_input.setEnabled(True)
            self.add_log("Server stopped")

    def update_stats(self, active_connections, total_requests):
        """Update statistics displays"""
        self.active_connections_lcd.display(active_connections)
        self.total_requests_lcd.display(total_requests)

    def add_log(self, message):
        """Add message to log display"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log_text.append(f"[{timestamp}] {message}")

    def add_connection(self, connection_data):
        """Add connection to connections table"""
        row_position = self.connections_table.rowCount()
        self.connections_table.insertRow(row_position)
        for i, data in enumerate(connection_data):
            self.connections_table.setItem(row_position, i, QTableWidgetItem(str(data)))

    def clear_logs(self):
        """Clear log display"""
        self.log_text.clear()

    def clear_connections(self):
        """Clear connections table"""
        self.connections_table.setRowCount(0)

    def closeEvent(self, event):
        """Handle application closure"""
        if self.proxy_worker:
            self.proxy_worker.stop()
        event.accept()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setStyle('Fusion')  # Modern look across platforms
    window = ProxyServerGUI()
    window.show()
    sys.exit(app.exec())