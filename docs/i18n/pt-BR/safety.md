> Esta página é uma tradução automática e pode conter erros. A [versão em inglês](https://github.com/ADCDS/quak/blob/main/docs/safety.md) é a referência. [Correções são bem-vindas](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations).

# O que o Quak muda, armazena e pode excluir

O Quak controla uma câmera real e pode mexer no Wi-Fi do seu computador. Esta página diz exatamente o que ele faz.

## No seu computador

**Bluetooth:** o Quak pareia com a câmera da forma normal, pelo Bluetooth do seu sistema. Ele não muda mais nada.

**Wi-Fi:** a prévia, a mídia e os downloads precisam da rede Wi-Fi da própria câmera.

- **Celular:** o Android se conecta só para o app, e seus outros apps continuam na sua rede normal.
- **Computador:** um adaptador Wi-Fi entra na rede da câmera por um tempo, e sai de qualquer rede em que estivesse até o Quak terminar.

Em um computador, o Quak:

- Prefere um adaptador pelo qual você não está online. Se o único adaptador for o que você está usando, o app pergunta antes, e a linha
  de comando para com um erro até você escolhê-lo com `--wifi-adapter NAME`.
- No Linux, cria um perfil temporário do NetworkManager chamado `quak-camap`, que nunca assume sua rota padrão nem seu DNS, e o
  exclui depois. Ele não edita seus outros perfis.
- No Linux, desativa a conexão automática nesse adaptador enquanto o usa, porque, do contrário, um perfil salvo da câmera poderia
  retomar o adaptador. A conexão automática é restaurada depois.
- Reconecta sua rede anterior quando termina. O app faz isso cerca de um minuto depois que você sai de Mídia ou para a prévia.

### Se o Quak for encerrado à força

A linha de comando faz a limpeza com Ctrl-C e com `kill`, e desfaz o que mudou. Se ele for encerrado de forma mais brusca, ou se a máquina
perder energia durante o uso, o Quak lembra o que mudou e desfaz na próxima vez que iniciar no Linux. No Windows, não há recuperação
automática.

Para recuperar manualmente no Linux, onde `DEVICE` é o seu adaptador Wi-Fi:

```bash
nmcli connection delete quak-camap
nmcli device set DEVICE autoconnect yes
nmcli device connect DEVICE
```

Reiniciar também desfaz a mudança na conexão automática, e só deixa o perfil `quak-camap` para excluir.

### O que fica armazenado

| O quê | Onde | Contém |
|---|---|---|
| Câmeras pareadas, configurações | `~/.config/quak` no Linux, `%APPDATA%\quak` no Windows, o armazenamento privado do app no Android | Endereços das câmeras, modelos, preferências do app |
| Downloads | `~/Videos/Quak` no app de desktop, o armazenamento privado do app no Android, e `--out` ou a pasta atual na linha de comando | Seus clipes |
| Dados de teste | `~/.local/state/quak` no Linux, `%LOCALAPPDATA%\quak` no Windows | Só quando você executa os conjuntos de testes |

O Quak não armazena suas senhas de Wi-Fi nem suas chaves de transmissão. Ele as envia para a câmera quando você pede para entrar em uma rede ou transmitir.

### O que aparece nos logs

A tela Diagnóstico, o log exportado dela e `quak -v` mostram os bytes trocados com a câmera. Eles omitem senhas, chaves de transmissão,
nomes de rede, o número de série da câmera e o nome do seu computador, e informam quantos bytes foram ocultados.

O endereço Bluetooth da câmera ainda pode aparecer em mensagens de erro e logs, então revise um log antes de compartilhá-lo publicamente.

### O acesso do app à rede

`quak-gui --web` serve o app em um navegador, apenas neste computador. **Não há login**, e qualquer pessoa que consiga acessá-lo pode
gravar na sua câmera e excluir arquivos dela, então `--host` só faz sentido em uma rede em que você confia totalmente.

## Na câmera

**O Quak informa à câmera o nome do seu computador** ao parear, como o nome que a câmera mostra para este dispositivo.

**Uma captura programada muda configurações e as deixa alteradas.** Programar uma:

- Muda a câmera para os controles Pro, porque a câmera esconde a captura programada no modo Fácil
- Carrega a predefinição de vídeo que você escolher
- Define a Duração do vídeo, o que **também limita as gravações que você inicia manualmente**
- Sincroniza o relógio da câmera

O Quak avisa quando programar uma captura muda essas configurações. Volte a Duração ao valor anterior nas configurações da câmera, ou no Quak, quando terminar.

**Transmissão ao vivo:**

- A câmera entra na rede Wi-Fi que você escolher e a armazena. Só **Redefinir conexões** (Reset Connections) na câmera remove uma rede
  armazenada, e isso também remove todos os pareamentos dela.
- A senha da sua rede e o URL da sua transmissão, que contém sua chave de transmissão, são enviados para a câmera por Bluetooth.
- Com um URL `rtmp://` simples, a chave de transmissão e o vídeo viajam sem criptografia até o servidor.

**O Wi-Fi da câmera** é ligado quando necessário, e desligado de novo quando o Quak termina, se foi o Quak que o ligou. Um app encerrado à força
durante a prévia pode deixá-lo ligado: execute `quak ap off`, ou desligue-o na câmera.

## O que o Quak pode excluir

Só os clipes que você escolher. Excluir um sempre pede confirmação no app, e exige `--yes` na linha de comando. Clipes excluídos
não podem ser recuperados.

O Quak não tem como formatar um cartão, excluir tudo ou restaurar uma câmera para os padrões de fábrica.

Os downloads nunca substituem um arquivo existente: um segundo `GX010001.MP4` é salvo como `GX010001_1.MP4`. Um download interrompido é
informado como erro, e não é salvo como se estivesse completo.

## Para desenvolvedores

`QUAK_DEV=1` mostra o autoteste do app e o modo de teste de Ao vivo. Eles mudam muito mais coisas, então leia [testing.md](https://github.com/ADCDS/quak/blob/main/docs/testing.md) primeiro:

- **Autoteste:** leva cerca de 30 minutos, grava clipes, muda configurações e as restaura, e usa o adaptador Wi-Fi.
- **Modo de teste:** transforma este computador em um hotspot, e a câmera armazena essa rede.
