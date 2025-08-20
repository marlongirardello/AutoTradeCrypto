# Usar uma imagem base oficial do Python
FROM python:3.10-slim

# Definir o diretório de trabalho dentro do contentor
WORKDIR /app

# Instalar as ferramentas de compilação necessárias
RUN apt-get update && apt-get install -y build-essential curl

# Instalar a linguagem Rust (necessária para as bibliotecas da Solana)
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

# Copiar o ficheiro de requisitos para o contentor
COPY requirements.txt .

# Instalar as bibliotecas Python
RUN pip install --no-cache-dir -r requirements.txt

# Copiar o resto do código da aplicação para o contentor
COPY . .

# Comando para executar a aplicação
CMD ["python", "AutoTradeCrypoBot.py"]
