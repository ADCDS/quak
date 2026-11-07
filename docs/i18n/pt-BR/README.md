> Esta página é uma tradução automática e pode conter erros. A [versão em inglês](https://github.com/ADCDS/quak/blob/main/README.md) é a referência. [Correções são bem-vindas](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations).

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/src/quak/gui/assets/logo.png" width="128" alt="Logo do Quak: um pato amarelo com uma lente de câmera no lugar do olho"></p>

<h1 align="center">Quak</h1>

<p align="center">Um controle remoto de código aberto para a GoPro HERO11 Black: uma ferramenta de linha de comando e um app para desktop e Android.</p>

<p align="center"><a href="https://github.com/ADCDS/quak/blob/main/README.md">English</a> · <b>Português (Brasil)</b> · <a href="../es/README.md">Español</a> · <a href="../fr/README.md">Français</a> · <a href="../de/README.md">Deutsch</a> · <a href="../ru/README.md">Русский</a> · <a href="../zh-CN/README.md">简体中文</a> · <a href="../ja/README.md">日本語</a></p>

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/hero.png" alt="O Quak no desktop e no celular, mostrando a prévia ao vivo de uma câmera simulada"></p>

O Quak se comunica com a câmera por Bluetooth e pelo próprio Wi-Fi da câmera, usando a
[API Open GoPro](https://gopro.github.io/OpenGoPro/) pública da GoPro. Sem conta e sem nuvem.

> **Status: fase inicial, testado em uma única câmera.** Foi feito em torno de uma HERO11 Black (firmware 2.20). Outros modelos
> recebem um aviso e podem não funcionar. Veja [o que foi testado](#o-que-foi-testado) antes de depender dele.

## Experimente sem câmera

O Quak inclui uma câmera simulada que fala o protocolo real, então você pode experimentar tudo primeiro. Você precisa do
Python 3.11 ou mais recente.

```bash
pipx install 'quak-camera[gui]'               # or pip, in a virtual environment

quak --sim info                               # the command line
quak --sim schedule arm +2m --duration 15s
quak-gui --sim                                # the app (the first launch downloads about 45 MB)
```

Ou baixe um app pronto para Linux, Windows ou Android em [Releases](https://github.com/ADCDS/quak/releases). O
[guia de instalação](install.md) traz os detalhes de cada sistema.

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/terminal.gif" width="720" alt="O comando quak lendo uma câmera simulada e programando uma captura"></p>

## O que ele faz

- **Controle remoto:** grave, tire fotos, marque HiLights, mude predefinições e qualquer configuração da HERO11.
- **Captura programada:** programe a câmera para um horário e uma duração, com o relógio sincronizado antes. Ela começa e para
  sozinha, mesmo que você a desligue depois de programar.
- **Prévia ao vivo** pelo Wi-Fi da câmera. A câmera não consegue transmitir enquanto grava, então a prévia pausa até você parar.
- **Mídia:** navegue pelo cartão, baixe clipes e exclua clipes, com uma confirmação que mostra o nome do arquivo.
- **No seu idioma:** o app segue o idioma do sistema, ou você pode escolher em **Mais**: English, Português (Brasil),
  Español, Français, Deutsch, Русский, 简体中文 ou 日本語. A linha de comando continua em inglês.
- **Transmissão ao vivo** para um servidor RTMP como YouTube ou Twitch. Isso é **experimental**: veja os [problemas conhecidos](troubleshooting.md#problemas-conhecidos).

<p align="center">
  <img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/phone-cameras.png" width="230" alt="A tela Câmeras, mostrando bateria, espaço no cartão e relógio">
  <img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/phone-schedule.png" width="230" alt="Uma captura programada ativa, com contagem regressiva">
  <img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/phone-settings.png" width="230" alt="Configurações da câmera, com as opções exclusivas do modo Pro em cinza e o motivo indicado">
</p>

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/desktop-media.png" width="760" alt="A tela Mídia no desktop: uma grade de clipes e os detalhes do clipe selecionado"></p>

As capturas de tela vêm da câmera simulada, com uma cena desenhada em vez de imagens reais.

## Use com a sua câmera

Instale como acima, mais o que [o guia de instalação](install.md) lista para o seu sistema. Depois, pareie uma vez. Na câmera, abra
**Preferências > Conexões > Conectar dispositivo > GoPro Quik (Preferences > Connections > Connect Device > GoPro Quik App)**, e deixe essa tela aberta enquanto você executa:

```bash
quak pair          # finds the camera on its pairing screen and remembers it
quak info
quak record --seconds 5
quak schedule arm +2m --duration 15s
quak media list
```

`quak --help` lista todos os comandos. No app, use **Adicionar câmera**, que guia você pelos mesmos passos.

**Android:** baixe o APK em [Releases](https://github.com/ADCDS/quak/releases). Ele precisa do Android 10 ou mais recente.

Antes de usar para valer, leia [o que o Quak muda no seu computador e na câmera](safety.md). Em resumo:

- Em um computador, ver a mídia ou a prévia passa um adaptador Wi-Fi para a rede da câmera por um tempo. O Quak evita o
  adaptador pelo qual você está online, e pergunta antes se ele for o único.
- Uma captura programada muda a configuração Duração da câmera e a deixa alterada, o que também limita as gravações que você
  inicia manualmente. O Quak avisa quando isso acontece.

<p align="center"><img src="https://raw.githubusercontent.com/ADCDS/quak/main/docs/images/how-it-connects.svg" width="760" alt="O Bluetooth leva os controles e as configurações. O próprio Wi-Fi da câmera leva a prévia e a mídia. Para uma transmissão ao vivo, a câmera entra no seu Wi-Fi."></p>

## O que foi testado

Testado em uma única HERO11 Black real, com os conjuntos de testes de ponta a ponta no Linux e manualmente em um celular Android:

| Recurso | Linux | Android | Windows |
|---|---|---|---|
| Pareamento e reconexão | Testado | Testado | Não testado |
| Status, sincronização do relógio | Testado | Testado | Não testado |
| Gravação | Testado | Testado | Não testado |
| HiLights, predefinições, configurações | Testado | Não testado | Não testado |
| Captura programada | Testado | Não testado | Não testado |
| Prévia ao vivo | Testado | Testado | Não testado |
| Mídia: listar, baixar, excluir | Testado | Testado | Não testado |
| Transmissão ao vivo | Experimental | Não testado | Não testado |

**O Windows é experimental.** A versão para Windows compila e os testes unitários dela passam, mas ela nunca foi aberta nem usada com
uma câmera. A transmissão ao vivo funcionou de ponta a ponta e depois parou de funcionar na câmera de teste, por um motivo ainda
desconhecido. [`docs/testing.md`](https://github.com/ADCDS/quak/blob/main/docs/testing.md) tem os detalhes.

## Documentação

- [Guia de instalação](install.md): Linux, Windows e Android, e como compilar os pacotes
- [Segurança](safety.md): o que o Quak muda e armazena, o que ele pode excluir e como recuperar
- [Solução de problemas](troubleshooting.md): problemas comuns e problemas conhecidos
- [Arquitetura](https://github.com/ADCDS/quak/blob/main/docs/architecture.md): como o código está organizado
- [Testes](https://github.com/ADCDS/quak/blob/main/docs/testing.md): os conjuntos de testes e os resultados em hardware
- [Notas sobre o protocolo](https://github.com/ADCDS/quak/blob/main/docs/protocol.md): o que aprendemos sobre a câmera, além da documentação oficial
- [Como contribuir](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md)

## Como foi feito

O Quak foi feito com muita ajuda de IA. O Claude escreveu a maior parte do código, dos testes, da documentação e do logo, e executou
os conjuntos de testes em uma câmera real, sob a direção de uma pessoa que também o experimentou na câmera e no celular. As afirmações
sobre hardware acima vêm desses testes, e de nada mais.

## Licença e aviso legal

MIT: veja [LICENSE](https://github.com/ADCDS/quak/blob/main/LICENSE). Os apps empacotados incluem outros softwares sob suas próprias licenças, incluindo bibliotecas LGPL: veja
[THIRD_PARTY_NOTICES.md](https://github.com/ADCDS/quak/blob/main/THIRD_PARTY_NOTICES.md).

O Quak é um projeto independente, não afiliado, endossado nem patrocinado pela GoPro, Inc. GoPro, HERO e Quik são
marcas comerciais da GoPro, Inc. Veja [NOTICE](https://github.com/ADCDS/quak/blob/main/NOTICE).
