FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ctranslate2<4.5 ships libctranslate2 with an executable-stack ELF flag that
# newer glibc/kernel combos refuse to grant at dlopen time. See fix_execstack.py.
RUN python3 fix_execstack.py "/usr/local/lib/python3.11/site-packages/ctranslate2.libs/libctranslate2*.so*"

# ctranslate2's wheel bundles only a cuDNN front-end stub, so the modular
# libcudnn_*_infer.so.8 libraries it lazily dlopens at first GPU inference must
# be resolvable at runtime. nvidia-cudnn-cu12 (+ transitive nvidia-cublas-cu12)
# installs them under site-packages/nvidia/*/lib; put those dirs on the loader
# path. See requirements.txt for the full rationale.
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib:/usr/local/lib/python3.11/site-packages/nvidia/cublas/lib

EXPOSE 8003
CMD ["python3", "main.py"]
