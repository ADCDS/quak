> Esta página é uma tradução automática e pode conter erros. A [versão em inglês](https://github.com/ADCDS/quak/blob/main/docs/install.md) é a referência. [Correções são bem-vindas](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations).

# Instalando o Quak

Você não precisa de uma câmera para experimentar: todo comando aceita `--sim`, que usa uma câmera simulada.

## Linux

Você precisa de:

- Python 3.11 ou mais recente
- Para uma câmera real: um adaptador Bluetooth e BlueZ (a maioria das distribuições desktop já tem), e NetworkManager com `nmcli`
  para entrar no Wi-Fi da câmera
- FFmpeg, para a prévia ao vivo no app de desktop e para `quak preview --play`, que também precisa de `ffplay`

```bash
pipx install 'quak-camera[gui]'     # or 'quak-camera' for just the command line
```

O pacote se chama `quak-camera` porque `quak` no PyPI é um projeto sem relação com este. Os comandos são `quak` e `quak-gui`.

Ou baixe o `.tar.gz` em [Releases](https://github.com/ADCDS/quak/releases), descompacte e execute `./quak`. Ele precisa da glibc 2.34 ou
mais recente (Ubuntu 22.04, Debian 12), GTK 3 e libmpv: no Debian e no Ubuntu, `sudo apt install libgtk-3-0 libmpv2`.

A primeira execução de `quak-gui` baixa o cliente de desktop do Flet, cerca de 45 MB, para `~/.flet`.

Ajuda ter um segundo adaptador Wi-Fi, como um USB barato, porque o adaptador que entra na câmera sai da rede em que estava.
Escolha com `--wifi-adapter NAME` ou `QUAK_WIFI_ADAPTER`. Com um único adaptador, o Quak pergunta antes.

## Windows (experimental)

Esta versão ainda não foi executada com uma câmera, nem sequer aberta, então espere problemas e, por favor, relate-os.

```powershell
py -m venv quak-env
quak-env\Scripts\activate
pip install "quak-camera[gui]"
```

Ou baixe o `.zip` em [Releases](https://github.com/ADCDS/quak/releases), descompacte e execute `quak.exe`. Ele não tem assinatura de código,
então o Windows pode exibir um aviso.

## Android

Baixe o APK em [Releases](https://github.com/ADCDS/quak/releases) e instale. O Android pede que você permita a instalação pelo
navegador ou gerenciador de arquivos. Ele precisa do Android 10 ou mais recente, e pede Bluetooth e Localização: o Android exige a
permissão de Localização para uma busca por Bluetooth.

Para confirmar que um APK veio deste projeto, compare o hash SHA-256 do certificado, obtido com `apksigner verify --print-certs`, com este:

```text
fec9c12dc325dd08272a9163c60e033519d74ebd98a31e133b7b145b259bd59b
```

Na primeira vez que o Quak entra no Wi-Fi da câmera, o Android pede que você confirme a conexão.

Os downloads ficam dentro do app até você tocar em um para compartilhar ou salvar. Desinstalar o app exclui os que você não compartilhou.

## Encontrando sua câmera

```bash
quak scan                   # lists cameras nearby, with their addresses
quak pair                   # pairs the one on its pairing screen
quak pair --address AA:BB:CC:DD:EE:FF    # or choose one
```

Para abrir a tela de pareamento na câmera: **Preferências > Conexões > Conectar dispositivo > GoPro Quik (Preferences > Connections > Connect Device > GoPro Quik App)**.

## Compilando os pacotes

A maioria das pessoas não precisa: as versões empacotadas estão na página [Releases](https://github.com/ADCDS/quak/releases), com um arquivo `SHA256SUMS`
para verificar os downloads. Para compilar a sua:

```bash
git clone https://github.com/ADCDS/quak.git && cd quak
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[gui]'
tools/build_app.sh linux      # needs clang, cmake, ninja, pkg-config, GTK 3 headers, liblzma and libmpv
tools/build_app.sh windows    # on Windows, from Git Bash
tools/build_app.sh apk        # needs Java 17
```

O Flet baixa o Flutter e o Android SDK na primeira vez. O resultado fica em `app/build/`.

Mantenedores também podem executar o workflow `build` na aba Actions do repositório.

APKs assinados com chaves diferentes não conseguem atualizar uns aos outros, então, para trocar de versão, desinstale a antiga primeiro. Isso
remove o pareamento do app e os downloads dele.
