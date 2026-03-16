On HOST:
tar czf n8n.tar.gz n8n-python-project/*

n8n_targets=(136 187 149)
for x in "$(n8n_targets[@])": do
    scp n8n.tar.gz user@192.168.0."$x":/home/user
done

On VM:
1. install ubuntu 24.04 server LTS on a VM
2. install docker
sudo su

systemctl status docker;

curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg;

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null;

sudo apt-get update;

apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin python3-pip;

systemctl enable --now docker;

systemctl status docker;

groupadd -f docker;

usermod -aG docker user;

tar xf n8n.tar.gz

cd n8n-python-project

docker compose build

docker compose up -d

chown -R 1000:1000 n8n-data/

docker restart n8n


## Notes
1. n8n is setup by default to be behind a TLS/HTTPS and use secure cookies. otherwise: in docker-compose.yml, at n8n's env variables: 
- N8N_SECURE_COOKIE=false

2. the directory n8n-data/ will be created by the n8n container, it's apparently made under root, and the container cannot access it. Becoming an error spiral. chown -R 1000:1000 n8n-data/ to set the owner to the user and move on.

3. Open a browser to:
http://<VM_IP>:5678

4. login with new password (the password written in the docker compose does not matter for humans)

5. create a workflow > trigger node (manual) > an HTTP Request node (this is going to link to python)

6. HTTP Method: POST

URL: http://python-api:8000/execute

    Important: inside Docker, n8n reaches Python via the service name python-api, not localhost.

Authentication: None (for now).

Send body: True

Type: JSON.

7. have scripts at "./scripts", sort out an rsync or cron job to sync the scripts folder with google drive

## For testing
Connect Manual Trigger → HTTP Request.

Click “Execute Workflow” (or execute the HTTP node).

