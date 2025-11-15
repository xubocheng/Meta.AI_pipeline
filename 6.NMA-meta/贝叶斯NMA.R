#做贝叶斯NMA我们需要用到2个R包，一个是gemtc包（做NMA的本体），另一个是rjags包（一种Gibbs采样器）
#其中rjags包需要先安装rjags软件，然后下载rjags包才能使用
install.packages("gemtc")#安装gemtc包，做贝叶斯NMA的本体
install.packages("rjags")#安装rjags包，一个Gibbs采样器，需要同时安装rjags软件
library("gemtc")#加载gemtc包
library("rjags")#加载rjags包
library("dmetar")#最好是有这个包，下载方式见前面的两两比较meta分析
library("showtext")
showtext_auto()
#设置工作路径，设置工作路径有2种办法，一种是直接把代码文件和数据放在同一个文件夹，另一种就是用下面的setwd()命令
setwd(".")
getwd()
#读取数据，<-在R中是赋值（定义）的意思，快捷键是ALT+-。所以下面这行代码的意思是“读取数据contion.csv，并将这份数据赋值给一个叫nmadata 的对象”
nmadata <- read.csv(".csv",header = T,sep = ",")
#R语言无法识别中文和一些特殊符号，所以建议数据中干预方式均使用数字代替，然后使用下面的命令设置干预方式的名称
treatments <- read.table(textConnection(
  'id description
    .
   '), header=T)
#将数据打包成gemtc可以识别分析的格式，下面这段代码的意思是“将nmadata和treatments打包进network”
network <- mtc.network(nmadata, #前面说过了，我们将数据文件读取后赋值给了nmadata
                       treatments = treatments)#前面说了，我们将干预方式命名然后赋值给了treatments
summary(network)#查看数据概要
#绘制网络证据图，这个图建议用stata绘制更好看，后面我会教怎么画
plot(network,#network是前面我们打包后的数据
     use.description = T,#表示是否使用我们定义在treatments里的标签
     vertex.label.cex=1,#表示干预方式文字大小的倍数
     vertex.size=nmadata$sampleSize, #表示将节点的大小设置为该干预方式对应的样本量，R中我们可以使用“数据集$列名“提取特定某列数据
     vertex.shape="circle", #节点的形状设置为圆形
     vertex.label.color="#226E9C", #标签的颜色，想换颜色的话改成颜色的十进制代码就行，去配色网站自己查
     vertex.label.dist=4, #设置顶点标签的距离
     vertex.label.degree=-pi/2, #设置顶点标签的角度
     vertex.color="green", #设置节点的颜色
     dynamic.edge.width=T,#将连线用直接比较的数量加权
     edge.color="gray", #将连线的颜色设置为灰色
     vertex.label.font=1)#设置顶点标签的字体类型
#建立贝叶斯模型，下面这段代码我强烈建议使用“?mtc.model”看看说明书，非常重要
#mtc.model函数是我们设置贝叶斯NMA的第一步，课件里我们说过贝叶斯需要设置先验信息，但是先验信息很多时候是没有的，设置起来难度也很大
#所以我们干脆直接使用mtc.model的默认设置“无信息先验”，让程序从我们的数据中自动总结信息
#mtc.model函数设置先验信息的命令是om.scale、hy.prior、re.prior.sd，感兴趣的话自己看看说明书研究下
model.ran <- mtc.model(network,#network是前面我们打包后的数据
                       n.chain=4,#n.chain用于设置马尔可夫链的数量，设置一般认为3-4就可以
                       likelihood="normal", #MD/SMD就设置normal，OR就设置binom,RR设置binom，HR设置poisson，HR（每组随访时间相等）设置binom
                       link="identity",#MD/SMD就设置identity，OR就设置logit,RR设置clog，HR设置log，HR（每组随访时间相等）设置cloglog
                       type="consistency", #type表示我们这里使用的是一致性模型（consistency），如果是不一致模型就设置（ume、use），回归模型就设置为（regression）
                       linearModel='random',#linearModel用于设置分析模型，fixed固定效应，random随机效应
                       dic=TRUE)#DIC表示是否输出DIC值
#对贝叶斯模型设置马尔科夫链蒙特卡洛抽样，记住下面的代码，我们现在把NMA模型赋值给了result.ran这个对象
result.ran <- mtc.run(model.ran,#model.ran是上一步我们设置的贝叶斯模型
                      n.adapt = 20000, #n.adapt设置退火次数，我个人习惯是设置20000，然后根据轨迹图密度图等改进
                      n.iter = 50000, #n.iter设置迭代次数，我个人习惯是设置50000，然后根据轨迹图密度图等改进
                      thin = 1)
summary(result.ran)#查看运行结果
#评估模型收敛情况——潜在尺度收缩因子（PSRF）
gelman.diag(result.ran)
#评估模型收敛情况——收敛诊断图（Brooks-Gelman-Rubin）
gelman.plot(result.ran)
pdf("收敛诊断图.PDF")
gelman.plot(result.ran)
dev.off()
#评估模型收敛情况——轨迹图和密度图
plot(result.ran)
pdf("轨迹图与密度图.PDF")
plot(result.ran)
dev.off()
#还有一种评估模型拟合的方法，原理是后验残差生成残差图（杠杆图）
plot(mtc.deviance(result.ran))#绘制残差图
pdf("杠杆图.PDF")
plot(mtc.deviance(result.ran))
dev.off()
#两两比较结果
#联赛表
relative.effect.table(result.ran)#查看联赛表
ls <- round(relative.effect.table(result.ran),digits = 2)#联赛表小数位数太多，我们使用round函数设置只保留2位小数（digits = 2）
print(ls)
write.csv(ls, "联赛表.csv")#将联赛表导出为csv
#森林图,我们可以通过写for循环，实现一次性输出所有森林图
pdf("森林图.pdf",6.5,3)
for (i in network$treatments$id) {
  forest(relative.effect(result.ran,i),
         use.description = T)#设置是否在图中显示研究标签
}
dev.off()
#排序结果
ranks <- rank.probability(result.ran, preferredDirection=1)#preferredDirection取1或-1，表示越大越好或越小越好
print(ranks)#概率排序值
sucra(ranks)#最重要的SUCRA排序值
plot(ranks,xlab='干预方式',ylab='堆积排序图')#堆积排序图
plot(ranks, ylim=c(0, 1), beside=TRUE,xlab='干预方式',ylab='堆积排序图')#分组堆积排序图
plot(sucra(ranks))#绘制SUCRA排序图
pdf("SUCRA排序图.pdf")
plot(sucra(ranks))
dev.off()
