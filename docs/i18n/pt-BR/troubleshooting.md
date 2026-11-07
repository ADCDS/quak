> Esta página é uma tradução automática e pode conter erros. A [versão em inglês](https://github.com/ADCDS/quak/blob/main/docs/troubleshooting.md) é a referência. [Correções são bem-vindas](https://github.com/ADCDS/quak/blob/main/CONTRIBUTING.md#translations).

# Solução de problemas

## Pareamento e conexão

**"Nenhuma câmera está na tela de pareamento" ("No camera is on its pairing screen").** Uma câmera ignora dispositivos que não conhece, a menos que esta tela esteja aberta: **Preferências > Conexões > Conectar dispositivo > GoPro Quik (Preferences > Connections > Connect Device > GoPro Quik App)**. Abra a tela e execute `quak pair` de novo.

**Parear o celular e depois o computador falha.** Conectar de outro dispositivo fecha a tela de pareamento da câmera. Pareie um
dispositivo por vez, e reabra a tela antes de parear o próximo.

**A câmera "não foi encontrada em 45 s" ("not seen within 45 s").** Verifique se ela está ligada e por perto. Quando não está pareando, uma HERO11 pode
se anunciar apenas a cada 10–20 segundos, e uma câmera em suspensão pode demorar mais. Tente de novo, ou aperte um botão na câmera para acordá-la.

**Ela conecta, mas nunca termina a configuração.** Uma vez, depois que uma câmera perdeu energia enquanto estava no USB, ela parou de
oferecer seus serviços Bluetooth até ser reiniciada. Desligue e ligue a câmera.

**A câmera se recusa a gravar ou mudar configurações, sem dar motivo.** Ela pode estar presa no modo Turbo Transfer, que bloqueia
gravação, configurações e prévia. O Quak ativa esse modo para downloads, então uma conexão que cai durante um download pode deixá-lo ativo. O app
o desativa na próxima vez que conecta, e a linha de comando avisa na mensagem de erro: execute `quak turbo off`.

## Erros de Bluetooth

**"O Bluetooth parou de responder" ("Bluetooth stopped responding").** Verifique se o Bluetooth está ligado. Se continuar acontecendo no Linux, veja o log do kernel:

```bash
journalctl -k | grep "tx timeout"
```

Uma linha como `command 0x2041 tx timeout`, seguida de uma desconexão USB, significa que o adaptador Bluetooth travou. O Quak não consegue
evitar isso. Um dongle USB Bluetooth 5.4 fez isso mais de 20 vezes em um dia, em portas diferentes, mesmo sem mais nada usando o dongle. O adaptador
volta desligado, então ligue-o com `bluetoothctl power on`. Se isso acontecer com frequência, experimente outro adaptador.

**"Não encontrada em 45 s" ("Not seen within 45 s") depois de um travamento do adaptador.** O mesmo dongle às vezes voltava dizendo que estava ligado, sem captar nada,
e sem nenhum erro no log. Se a sua câmera está ligada e nada mais por perto aparece em uma busca por Bluetooth, desligue e ligue o Bluetooth
de novo: `bluetoothctl power off`, depois `bluetoothctl power on`.

## Wi-Fi, mídia e prévia

**"O adaptador Wi-Fi X é a conexão de rede deste computador" ("Wi-Fi adapter X is this computer's network connection").** O Quak não toma a sua conexão sem perguntar. Conecte um segundo
adaptador, ou permita com `--wifi-adapter X`. No app, ele pergunta a você.

**"Conectando ao Wi-Fi da câmera" ("Joining the camera's Wi-Fi") demora muito no celular.** Se o Wi-Fi da câmera estava desligado, o celular pode levar 30 segundos ou mais
para vê-lo.

**O celular diz que não entrou, "o pedido de conexão foi recusado?" ("was the Connect prompt declined?")** O Android pergunta uma vez antes de entrar na rede da câmera. Se
você não viu o pedido, tente de novo: também pode significar que o celular não encontrou a rede a tempo.

**A prévia está em branco.** A câmera interrompe a transmissão enquanto grava, e é por isso que o painel avisa. Depois que você para de gravar, ou
depois que muda de modo, a transmissão volta em cerca de 10 segundos e a imagem em cerca de 20.

**"A câmera não iniciou a prévia" ("The camera didn't start its preview").** A câmera recusou. Tente de novo em instantes. O Quak já limpa uma transmissão deixada em andamento por
um app encerrado à força.

**"Nenhuma imagem chegou da câmera" ("No picture came from the camera").** O Quak iniciou a prévia, mas nenhum vídeo chegou em 30 segundos. Tente de novo. Se continuar
acontecendo em um computador, verifique se um firewall permite UDP de entrada na porta 8554, que é para onde a câmera envia o vídeo.

**O Wi-Fi da câmera continua ligado.** Um app encerrado à força durante a prévia não consegue desligá-lo. Execute `quak ap off`.

**Meu computador perdeu o Wi-Fi depois que o Quak foi encerrado à força.** Veja [Se o Quak for encerrado à força](safety.md#se-o-quak-for-encerrado-à-força).

## O app

**`quak-gui` diz que não há tela.** Você está em uma máquina sem tela, como por SSH. `quak-gui --web` serve o app para um navegador
na mesma máquina.

**A primeira execução parece travar.** O cliente de desktop tem cerca de 45 MB e é baixado uma vez.

**O app Android não atualiza.** APKs assinados com chaves diferentes não conseguem atualizar uns aos outros. Desinstale o antigo primeiro, o que
remove o pareamento e os downloads dele.

## Problemas conhecidos

**A transmissão ao vivo pode travar ao entrar em uma rede (experimental).** A câmera se conecta à rede Wi-Fi e conclui o handshake,
mas nunca pede um endereço IP, então a entrada na rede expira depois de 60 segundos. A transmissão ao vivo funcionou de ponta a ponta na câmera de teste, inclusive
duas vezes no dia em que isso começou, e depois todas as tentativas seguintes travaram. Na câmera afetada:

| Tentativa | Resultado |
|---|---|
| Suspender e acordar a câmera | Continua travando |
| Reiniciar a câmera | Continua travando |
| Desconectar e reconectar o adaptador Wi-Fi do computador | Continua travando |
| Um nome de rede que a câmera nunca tinha visto | Continua travando |
| Um celular entrando na mesma rede | Funciona |

A rede em si está bem, então o silêncio é do lado da câmera. Não se sabe por quê, nem se algo que o Quak enviou causou isso.
**Redefinir conexões** (Reset Connections) na câmera não foi testado, porque também remove todos os pareamentos. Se você encontrar esse problema ou conseguir contorná-lo, por favor
[abra uma issue](https://github.com/ADCDS/quak/issues).

**Um toque pode ocasionalmente não ser registrado.** Algumas telas são redesenhadas por completo quando a câmera informa uma mudança, e um toque nesse
momento se perde. Toque de novo. Mídia e Diagnóstico não fazem mais isso.

**O Windows nunca foi executado com uma câmera.** É experimental, então, por favor, relate o que encontrar.
