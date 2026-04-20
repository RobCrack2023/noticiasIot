# Deploy TechPulse en Ubuntu Server + Apache2 + HTTPS

---

## 1. Requisitos previos
- Ubuntu 22.04 LTS (VPS o dedicado)
- Un dominio apuntando a la IP del servidor (registro A: `tudominio.com → IP`)
- Acceso SSH como root o usuario con sudo

---

## 2. Actualizar el sistema e instalar dependencias

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git python3 python3-pip python3-venv apache2 certbot python3-certbot-apache ufw
```

---

## 3. Crear usuario para la app

```bash
sudo useradd -m -s /bin/bash techpulse
sudo su - techpulse
```

---

## 4. Clonar el proyecto desde GitHub

```bash
git clone https://github.com/RobCrack2023/noticiasIot.git /home/techpulse/noticias_iotRobotics
```

---

## 5. Instalar dependencias Python

```bash
cd /home/techpulse/noticias_iotRobotics

python3 -m venv venv
source venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt
```

Verificar que arranca:
```bash
uvicorn main:app --host 127.0.0.1 --port 8500
# Ctrl+C para detener
```

---

## 6. Configurar systemd (servicio permanente)

Sal del usuario techpulse (`exit`) y crea el servicio:

```bash
sudo nano /etc/systemd/system/techpulse.service
```

Contenido:
```ini
[Unit]
Description=TechPulse - Noticias IoT & Robotica
After=network.target

[Service]
User=techpulse
WorkingDirectory=/home/techpulse/noticias_iotRobotics
ExecStart=/home/techpulse/noticias_iotRobotics/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8500 --workers 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Activar y arrancar:
```bash
sudo systemctl daemon-reload
sudo systemctl enable techpulse
sudo systemctl start techpulse
sudo systemctl status techpulse   # debe verse "active (running)"
```

---

## 7. Configurar Apache2 como reverse proxy

Habilitar módulos necesarios:
```bash
sudo a2enmod proxy proxy_http proxy_balancer lbmethod_byrequests headers rewrite
sudo systemctl restart apache2
```

Crear el virtualhost:
```bash
sudo nano /etc/apache2/sites-available/techpulse.conf
```

Contenido:
```apache
<VirtualHost *:80>
    ServerName tudominio.com
    ServerAlias www.tudominio.com

    ProxyPreserveHost On
    ProxyPass /static/ /home/techpulse/noticias_iotRobotics/static/
    ProxyPassReverse /static/ /home/techpulse/noticias_iotRobotics/static/

    ProxyPass / http://127.0.0.1:8500/
    ProxyPassReverse / http://127.0.0.1:8500/

    RequestHeader set X-Forwarded-Proto "http"
    RequestHeader set X-Real-IP %{REMOTE_ADDR}s
</VirtualHost>
```

Activar el sitio:
```bash
sudo a2ensite techpulse.conf
sudo a2dissite 000-default.conf   # deshabilita el default si no lo usas
sudo apache2ctl configtest         # debe decir "Syntax OK"
sudo systemctl reload apache2
```

---

## 8. HTTPS con Let's Encrypt + Certbot

```bash
sudo certbot --apache -d tudominio.com -d www.tudominio.com
```

Certbot configura SSL automáticamente y agrega la redirección HTTP → HTTPS.

Verificar renovación automática:
```bash
sudo certbot renew --dry-run   # debe decir "Congratulations"
```

---

## 9. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Apache Full'   # abre puertos 80 y 443
sudo ufw enable
sudo ufw status
```

---

## 10. Verificar todo

```bash
# Estado del servicio
sudo systemctl status techpulse

# Logs en tiempo real
sudo journalctl -u techpulse -f

# Apache
sudo systemctl status apache2
```

Abre `https://tudominio.com` en el navegador — debe cargar con candado verde.

---

## Comandos útiles post-deploy

```bash
# Actualizar el código desde GitHub
sudo su - techpulse
cd noticias_iotRobotics
git pull origin master
source venv/bin/activate
pip install -r requirements.txt   # por si hay nuevas dependencias
exit

sudo systemctl restart techpulse

# Ver logs en tiempo real
sudo journalctl -u techpulse -f

# Ver últimos 100 logs
sudo journalctl -u techpulse -n 100 --no-pager
```

---

## Resumen del stack

```
Internet → Apache2 (80/443, SSL) → Uvicorn (127.0.0.1:8000) → FastAPI
```
