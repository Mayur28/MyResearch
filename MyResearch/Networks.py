import torch
import torch.nn as nn
import torch.nn.functional as F
import random
from torch.autograd import Variable
from ManageData import TensorToImage
import os
from collections import OrderedDict
import functools
import numpy as np
import glob

# Initializes the weights to have mean= 0, and std = 0.02 as advised by Radford
def weights_init(model):
    class_name = model.__class__.__name__
    if class_name.find('Conv') != -1:
        torch.nn.init.normal_(model.weight.data, 0.0, 0.02)
    elif class_name.find('BatchNorm2d') != -1:
        torch.nn.init.normal_(model.weight.data, 1.0, 0.02)
        torch.nn.init.constant_(model.bias.data, 0.0)

# Adds padding to the input image to ensure the input has the correct size to be passed to the network
# (In particular, the architecture seems quite sensitive to the size of the input image, regardless, 512x512 should be large enough)
# If the model needs to be able to handle images with size between 512 -> 1024, num_downs should be changed to 10 to accomodate for this change
def add_padding(input):
    height, width = input.shape[2], input.shape[3]

    optimal_size = 512 # 2^(num_downs) i.e, will be 1024 if num_downs = 10, etc
    pad_left = pad_right = pad_top = pad_bottom = 0
    if width != optimal_size:
        width_diff = optimal_size - width
        pad_left = int(np.ceil(width_diff / 2))
        pad_right = width_diff - pad_left
    if height != optimal_size:
        height_diff = optimal_size - height
        pad_top = int(np.ceil(height_diff / 2))
        pad_bottom = height_diff - pad_top

    padding = nn.ReflectionPad2d((pad_left, pad_right, pad_top, pad_bottom))
    input = padding(input)
    return input, pad_left, pad_right, pad_top, pad_bottom

# Removes the padding once the images have been enhanced
def remove_padding(input, pad_left, pad_right, pad_top, pad_bottom):
    height, width = input.shape[2], input.shape[3]
    return input[:, :, pad_top:height - pad_bottom, pad_left:width - pad_right]

class The_Model:  # This is the grand model that encompasses everything ( the generator, both discriminators and the VGG network)
    def __init__(self, opt):

        self.opt = opt
        # I'm assuming that a CUDA GPU is used.
        self.input_A = torch.cuda.FloatTensor(opt.batch_size, 3, opt.crop_size, opt.crop_size)  # Tensor that will hold the input low-light images
        self.input_B = torch.cuda.FloatTensor(opt.batch_size, 3, opt.crop_size, opt.crop_size) # Tensor that will hold the normal-light images
        self.input_A_gray = torch.cuda.FloatTensor(opt.batch_size, 1, opt.crop_size, opt.crop_size) # Tensor that will hold the illumination maps

        self.vgg_loss = PerceptualLoss()
        self.vgg_loss.cuda()  # --> Shift to the GPU

        self.vgg = load_vgg(self.opt.gpu_ids)  # This is for data parallelism
        self.vgg.eval()  # We call eval() when some layers within the self.vgg network behave differently during training and testing... This will not be trained (Its frozen!)!
        # The eval function is often used as a pair with the requires.grad or torch.no grad functions (which makes sense)

        for weights in self.vgg.parameters():
            weights.requires_grad = False  # The weights of vgg should not be trainable and we should not waste computation attempting to compute gradients for the VGG network


        self.Gen = make_G(opt)
        if self.opt.phase == 'test':
            self.load_model(self.Gen, 'Gener') # Will automatically load the latest generator model
            self.Gen.eval()

        if self.opt.phase == 'train':
            self.old_lr = opt.lr
            self.G_Disc = make_Disc(opt, False)
            self.L_Disc = make_Disc(opt, True)

            self.model_loss = GANLoss()

            self.G_optimizer = torch.optim.Adam(self.Gen.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))
            self.G_Disc_optimizer = torch.optim.Adam(self.G_Disc.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))
            self.L_Disc_optimizer = torch.optim.Adam(self.L_Disc.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))


    def forward(self):

        self.real_A = Variable(self.input_A)  # Variable is basically a tensor (which represents a node in the comp. graph) and is part of the autograd package to easily compute gradients
        self.real_B = Variable(self.input_B)  # This contains the normal-light images ( sort of our reference images)
        self.real_A_gray = Variable(self.input_A_gray)  # This is the attention map


        the_input = torch.cat([self.real_A, self.real_A_gray], 1)
        self.fake_B = self.Gen.forward(the_input)  # We forward prop. a batch at a time, not individual images in the batch!


    def update_learning_rate(self): # Linearly decays the learning rate to 0 over the final 50 epochs

        lrd = self.opt.lr / self.opt.niter_decay
        lr = self.old_lr - lrd
        self.old_lr = lr

        for param_group in self.G_optimizer.param_groups:
            param_group['lr'] = lr
        for param_group in self.G_Disc_optimizer.param_groups:
            param_group['lr'] = lr
        for param_group in self.L_Disc_optimizer.param_groups:
            param_group['lr'] = lr

    def set_input(self, input):
        input_A = input['A']
        input_B = input['B']
        input_A_gray = input['A_gray']

        # Copy the data to there respective cuda Tensors used for training on the GPU
        self.input_A.resize_(input_A.size()).copy_(input_A)
        self.input_B.resize_(input_B.size()).copy_(input_B)
        self.input_A_gray.resize_(input_A_gray.size()).copy_(input_A_gray)

    def perform_update(self):  # Do the forward,backprop and update the weights
        # This is for optimizing the generator.
        self.forward()  # This produces the fake samples and sets up some of the variables that we need ie. we initialize the fake patch and the list of patches.
        self.G_optimizer.zero_grad()
        self.Gen_Backprop()
        self.G_optimizer.step() # Perform the necessary optimization pertaining to the generator

        # Now onto updating the discriminator!
        self.G_Disc_optimizer.zero_grad()
        self.Global_Disc_Backprop()
        self.L_Disc_optimizer.zero_grad()
        self.Local_Disc_Backprop()
        self.G_Disc_optimizer.step()
        self.L_Disc_optimizer.step()

    def predict(self):
        self.real_A = Variable(self.input_A)
        self.real_A.requires_grad = False
        self.real_A_gray = Variable(self.input_A_gray)
        self.real_A_gray.requires_grad = False
        the_input = torch.cat([self.real_A, self.input_A_gray], 1)
        self.fake_B = self.Gen.forward(the_input)

    def Gen_Backprop(self):
        # First let the discriminator make a prediction on the fake samples
        # This is the part recommended by Radford where we test real and fake samples in stages
        pred_fake = self.G_Disc.forward(self.fake_B)
        pred_real = self.G_Disc.forward(self.real_B)

        self.Gen_adv_loss = (self.model_loss(pred_real - torch.mean(pred_fake), False) + self.model_loss(pred_fake - torch.mean(pred_real), True)) / 2
        # In a seperate variable, we start accumulating the loss from the different aspects (which include the loss on the patches and the vgg loss)

        w = self.real_A.size(3)
        h = self.real_B.size(2)

        self.fake_patch_list = []
        self.real_patch_list = []
        self.input_patch_list = []

        # Prepare the cropped images for the local discriminator so that we can just read from the appropriate data container when required
        for i in range(self.opt.num_patches):
            w_offset = random.randint(0, max(0, w - self.opt.patch_size - 1))
            h_offset = random.randint(0, max(0, h - self.opt.patch_size - 1))

            self.fake_patch_list.append(self.fake_B[:, :, h_offset:h_offset + self.opt.patch_size, w_offset:w_offset + self.opt.patch_size])
            self.real_patch_list.append(self.real_B[:, :, h_offset:h_offset + self.opt.patch_size, w_offset:w_offset + self.opt.patch_size])
            self.input_patch_list.append(self.real_A[:, :, h_offset:h_offset + self.opt.patch_size, w_offset:w_offset + self.opt.patch_size])

        accum_gen_loss = 0
        pred_fake_patch_list = 0
        # Perform the predictions on the list of patches
        for i in range(self.opt.num_patches):
            w_offset = random.randint(0, max(0, w - self.opt.patch_size - 1))
            h_offset = random.randint(0, max(0, h - self.opt.patch_size - 1))

            pred_fake_patch_list = self.L_Disc.forward(self.fake_patch_list[i])

            accum_gen_loss += self.model_loss(pred_fake_patch_list, True)
        self.Gen_adv_loss += accum_gen_loss / float(self.opt.num_patches)

        self.total_vgg_loss = self.vgg_loss.compute_vgg_loss(self.vgg, self.fake_B, self.real_A) * 1.0  # This the vgg loss for the entire images!

        patch_loss_vgg = 0
        for i in range(self.opt.num_patches):
            patch_loss_vgg += self.vgg_loss.compute_vgg_loss(self.vgg, self.fake_patch_list[i], self.input_patch_list[i]) * 1.0

        self.total_vgg_loss += patch_loss_vgg / float(self.opt.num_patches)

        self.Gen_loss = self.Gen_adv_loss + self.total_vgg_loss
        self.Gen_loss.backward()  # Compute the gradients of the generator using the sum of the adv loss and the vgg loss.

    # This is invoked when we update both the global and local discriminator
    def Shared_Disc_Backprop(self, network, real, fake, is_global):
        pred_real = network.forward(real)
        pred_fake = network.forward(
            fake.detach())

        if (is_global):
            Disc_loss = (self.model_loss(pred_real - torch.mean(pred_fake), True) +
                         self.model_loss(pred_fake - torch.mean(pred_real), False)) / 2
        else:
            loss_D_real = self.model_loss(pred_real, True)
            loss_D_fake = self.model_loss(pred_fake, False)
            Disc_loss = (loss_D_real + loss_D_fake) * 0.5
        return Disc_loss

    # Global discriminator backprop
    def Global_Disc_Backprop(self):
        self.G_Disc_loss = self.Shared_Disc_Backprop(self.G_Disc, self.real_B, self.fake_B, True)
        self.G_Disc_loss.backward()

    # Local discriminator backprop
    def Local_Disc_Backprop(self):
        L_Disc_loss = 0

        for i in range(self.opt.num_patches):
            L_Disc_loss += self.Shared_Disc_Backprop(self.L_Disc, self.real_patch_list[i], self.fake_patch_list[i], False)
        self.L_Disc_loss = L_Disc_loss / float(self.opt.num_patches)
        self.L_Disc_loss.backward()

    def get_model_errors(self, epoch):
        Gen = self.Gen_loss.item()
        Global_disc = self.G_Disc_loss.item()
        Local_disc = self.L_Disc_loss.item()
        vgg = self.total_vgg_loss.item() / 1.0
        return OrderedDict([('Gen', Gen), ('G_Disc', Global_disc), ('L_Disc', Local_disc), ('vgg', vgg)])

    def for_displaying_images(self):  # Since self.realA_ was declared as a Variable, .data extracts the tensor of the variable.
        real_A = TensorToImage(self.real_A.data)  # The low-light image (which is also our input image)
        fake_B = TensorToImage(self.fake_B.data)  # Our produced result
        self_attention = TensorToImage(self.real_A_gray.data)
        return OrderedDict([('real_A', real_A), (
            'fake_B', fake_B)])  # , , ('latent_real_A', latent_real_A),('latent_show', latent_show), ('real_patch', real_patch),('fake_patch', fake_patch),('self_attention', self_attention)])

    # Save the network periodically
    def save_network(self, network, label, epoch):
        save_name = '%s_net_%s.pth' % (epoch, label)
        save_path = os.path.join(self.opt.save_dir, save_name)
        torch.save(network.cpu().state_dict(), save_path)
        network.cuda(device=self.opt.gpu_ids[0])

    def save_model(self, label):
        self.save_network(self.Gen, 'Gener', label)
        self.save_network(self.G_Disc, 'Global_Disc', label)
        self.save_network(self.L_Disc, 'Local_Disc', label)

    # Function that finds the latest model to load when we are training
    def load_model(self, network, network_name):
        list_of_files = glob.glob(str(self.opt.save_dir) + "/*")  # * means all if need specific format then *.csv
        res = list(filter(lambda x: network_name in x, list_of_files))
        latest_file = max(res, key=os.path.getctime)
        loaded_file_path = os.path.join(self.opt.save_dir, latest_file)
        network.load_state_dict(torch.load(loaded_file_path))


def make_G(opt):
    generator = UnetGenerator(opt)
    generator.cuda(device=opt.gpu_ids[0])  # Transfer the generator to the GPU
    generator = torch.nn.DataParallel(generator, opt.gpu_ids)  # We only need this when we have more than one GPU
    generator.apply(weights_init)  # The weight initialization
    return generator


def make_Disc(opt, patch):
    discriminator = PatchGAN(opt, patch)
    discriminator.cuda(device=opt.gpu_ids[0])  #Load the model into the GPU
    discriminator = torch.nn.DataParallel(discriminator, opt.gpu_ids)  # Split the input across all the GPU's (if applicable)
    discriminator.apply(weights_init)
    return discriminator


class GANLoss(nn.Module): # We are using LSGAN loss which builds upon MSELoss
    def __init__(self):
        super(GANLoss, self).__init__()
        self.Tensor = torch.cuda.FloatTensor
        self.loss = nn.MSELoss()

    def __call__(self, input, target_is_real):
        target_tensor = self.Tensor(input.size()).detach().fill_(float(target_is_real))
        return self.loss(input, target_tensor)  # We then perform MSE on this!


def get_norm_layer(norm_type='instance'):
    if norm_type == 'batch':
        norm_layer = functools.partial(nn.BatchNorm2d, affine=True)
    elif norm_type == 'instance':
        norm_layer = functools.partial(nn.InstanceNorm2d, affine=False)
    return norm_layer


class MinimalUnet(nn.Module):
    def __init__(self, down=None, up=None, submodule=None, withoutskip=False, **kwargs):
        super(MinimalUnet, self).__init__()

        self.down = nn.Sequential(*down)
        self.sub = submodule
        self.up = nn.Sequential(*up)
        self.withoutskip = withoutskip
        self.is_sub = not submodule == None  # Will be false only for the innermost

    def forward(self, x, mask=None):
        if self.is_sub:  # Almost recursive in a way
            x_up, _ = self.sub(self.down(x), mask)
        else:  # If it is the inner-most (this would be the base case of the recursion)
            x_up = self.down(x)

        result = self.up(x_up)

        if self.withoutskip:  # No skip connections are used for the outer layer
            x_out = result
        else:
            x_out = (torch.cat([x, result], 1), mask)

        return x_out


# Defines the Unet generator.
# |num_downs|: number of downsamplings in UNet. For example,
# if |num_downs| == 7, image of size 128x128 will become of size 1x1
# at the bottleneck
class UnetGenerator(nn.Module):
    def __init__(self, opt):
        ngf = 64
        super(UnetGenerator, self).__init__()

        norm_type = get_norm_layer(opt.norm_type)
        # construct unet structure
        unet_block = UnetSkipConnectionBlock(ngf * 8, ngf * 8, submodule=None, position='innermost', norm_layer=norm_type)
        for i in range(opt.num_downs - 5):
            unet_block = UnetSkipConnectionBlock(ngf * 8, ngf * 8, submodule=unet_block, norm_layer=norm_type)
        unet_block = UnetSkipConnectionBlock(ngf * 4, ngf * 8, submodule=unet_block, norm_layer=norm_type)
        unet_block = UnetSkipConnectionBlock(ngf * 2, ngf * 4, submodule=unet_block, norm_layer=norm_type)
        unet_block = UnetSkipConnectionBlock(ngf, ngf * 2, submodule=unet_block, norm_layer=norm_type)
        unet_block = UnetSkipConnectionBlock(3, ngf, submodule=unet_block, position='outermost', norm_layer=norm_type)  # This is the outermost
        self.model = unet_block

    def forward(self, input):
        input, pad_left, pad_right, pad_top, pad_bottom = add_padding(input)
        latent = self.model(input[:, 0:3, :, :], input[:, 3:4, :, :])  # Extraction is correct!
        latent = remove_padding(latent, pad_left, pad_right, pad_top, pad_bottom)
        input = remove_padding(input, pad_left, pad_right, pad_top, pad_bottom)
        return input[:, 0:3, :, :] + latent


# Defines the submodule with skip connection.
# X -------------------identity---------------------- X
#   |-- downsampling -- |submodule| -- upsampling --|
class UnetSkipConnectionBlock(nn.Module):
    def __init__(self, outer_nc, inner_nc,
                 submodule=None, position='intermediate', norm_layer=nn.BatchNorm2d):
        super(UnetSkipConnectionBlock, self).__init__()
        input_nc = outer_nc

        downconv = nn.Conv2d(input_nc, inner_nc, kernel_size=4, stride=2, padding=1)
        downrelu = nn.LeakyReLU(0.2, True)
        downnorm = norm_layer(inner_nc)
        uprelu = nn.ReLU(True)
        upnorm = norm_layer(outer_nc)

        if position == 'outermost':
            # up_conv= nn.ConvTranspose2d(2*inner_nc, outer_nc,kernel_size=4, stride=2, padding=1) #--> These were initially problematic as it produced a checkerboard effect
            upsample = nn.UpsamplingBilinear2d(scale_factor=2)
            reflect = nn.ReflectionPad2d(1)
            up_conv = nn.Conv2d(2 * inner_nc, outer_nc, kernel_size=3, stride=1, padding=0)
            down = [downconv]
            up = [uprelu, upsample, reflect, up_conv, nn.Tanh()]
            # up = [uprelu, up_conv,nn.Tanh()]
            model = MinimalUnet(down, up, submodule, withoutskip=True)
        elif position == 'innermost':
            upsample = nn.UpsamplingBilinear2d(scale_factor=2)
            down = [downrelu, downconv]
            up = [uprelu, upsample, upnorm]
            model = MinimalUnet(down, up)
        else:
            upsample = nn.UpsamplingBilinear2d(scale_factor=2)
            reflect = nn.ReflectionPad2d(1)
            up_conv = nn.Conv2d(2 * inner_nc, outer_nc, kernel_size=3, stride=1, padding=0)
            # up_conv= nn.ConvTranspose2d(2*inner_nc, outer_nc,kernel_size=4, stride=2, padding=1,bias=use_bias)
            up = [uprelu, upsample, reflect, up_conv, upnorm]
            down = [downrelu, downconv, downnorm]
            # up = [uprelu,up_conv,upnorm]

            model = MinimalUnet(down, up, submodule)

        self.model = model

    def forward(self, x, mask=None):
        return self.model(x, mask)


class PatchGAN(nn.Module): # We include batch normalization in the discriminator in an attempt to improve stability and performance
    def __init__(self, opt, patch):
        super(PatchGAN, self).__init__()

        self.opt = opt
        if patch:
            no_layers = self.opt.num_patch_disc_layers
        else:
            no_layers = self.opt.num_disc_layers

        ndf = 64
        # Needs to be treated seperately (as advised by Radford - we dont apply on output of generator and input of discriminator)
        sequence = [nn.Conv2d(3, ndf, kernel_size=4, stride=2, padding=2),
                    nn.LeakyReLU(0.2, True)]

        nf_mult = 1
        nf_mult_prev = 1
        for n in range(1, no_layers):
            nf_mult_prev = nf_mult
            nf_mult = min(2 ** n, 8)
            sequence += [nn.Conv2d(ndf * nf_mult_prev, ndf * nf_mult, kernel_size=4, stride=2, padding=2),
                         nn.BatchNorm2d(ndf * nf_mult),
                         nn.LeakyReLU(0.2, True)]

        nf_mult_prev = nf_mult
        nf_mult = min(2 ** no_layers, 8)
        sequence += [nn.Conv2d(ndf * nf_mult_prev, ndf * nf_mult, kernel_size=4, stride=1, padding=2),
                     nn.BatchNorm2d(ndf * nf_mult),
                     nn.LeakyReLU(0.2, True)]

        sequence += [nn.Conv2d(ndf * nf_mult, 1, kernel_size=4, stride=1, padding=2)]

        self.model = nn.Sequential(*sequence)

    def forward(self, input):
        return self.model(input)  # <-- pass through the discriminator itself which is represented by self.model


class PerceptualLoss(nn.Module):
    def __init__(self):
        super(PerceptualLoss, self).__init__()
        self.instance_norm = nn.InstanceNorm2d(512, affine=False)  # This is to stabilize training
        # 512 is the number of features

    def compute_vgg_loss(self, vgg_network, image, target): # This is where we calculate the perceptual loss
        image_vgg = vgg_preprocess(image)
        target_vgg = vgg_preprocess(target)
        # The is precisely where we are calling forward on the vgg network
        img_feature_map = vgg_network(image_vgg)  # Get the feature map of the input image
        target_feature_map = vgg_network(target_vgg)  # Get the feature of the target image

        return torch.mean((self.instance_norm(img_feature_map) - self.instance_norm(target_feature_map)) ** 2)  # The actual Perceptual Loss calculation

class Vgg(nn.Module):

    def __init__(self):
        super(Vgg, self).__init__()

        self.conv1_1 = nn.Conv2d(3, 64, kernel_size=3, stride=1, padding=1)
        self.conv1_2 = nn.Conv2d(64, 64, kernel_size=3, stride=1, padding=1)

        self.conv2_1 = nn.Conv2d(64, 128, kernel_size=3, stride=1, padding=1)
        self.conv2_2 = nn.Conv2d(128, 128, kernel_size=3, stride=1, padding=1)

        self.conv3_1 = nn.Conv2d(128, 256, kernel_size=3, stride=1, padding=1)
        self.conv3_2 = nn.Conv2d(256, 256, kernel_size=3, stride=1, padding=1)
        self.conv3_3 = nn.Conv2d(256, 256, kernel_size=3, stride=1, padding=1)

        self.conv4_1 = nn.Conv2d(256, 512, kernel_size=3, stride=1, padding=1)
        self.conv4_2 = nn.Conv2d(512, 512, kernel_size=3, stride=1, padding=1)
        self.conv4_3 = nn.Conv2d(512, 512, kernel_size=3, stride=1, padding=1)

        self.conv5_1 = nn.Conv2d(512, 512, kernel_size=3, stride=1, padding=1)
        self.conv5_2 = nn.Conv2d(512, 512, kernel_size=3, stride=1, padding=1)
        self.conv5_3 = nn.Conv2d(512, 512, kernel_size=3, stride=1, padding=1)

    def forward(self, input):
        act = F.relu(self.conv1_1(input), inplace=True)
        act = F.relu(self.conv1_2(act), inplace=True)
        act = F.max_pool2d(act, kernel_size=2, stride=2)

        act = F.relu(self.conv2_1(act), inplace=True)
        act = F.relu(self.conv2_2(act), inplace=True)
        act = F.max_pool2d(act, kernel_size=2, stride=2)

        act = F.relu(self.conv3_1(act), inplace=True)
        act = F.relu(self.conv3_2(act), inplace=True)
        act = F.relu(self.conv3_3(act), inplace=True)
        act = F.max_pool2d(act, kernel_size=2, stride=2)

        act = F.relu(self.conv4_1(act), inplace=True)
        act = F.relu(self.conv4_2(act), inplace=True)
        act = F.relu(self.conv4_3(act), inplace=True)

        relu5_1 = F.relu(self.conv5_1(act), inplace=True)
        return relu5_1


def vgg_preprocess(batch):
    tensortype = type(batch.data)
    (r, g, b) = torch.chunk(batch, 3, dim=1)
    # We are having to perform this odd transformation because of the way the loaded vgg network was trained
    batch = torch.cat((b, g, r), dim=1)  # convert RGB to BGR
    batch = (batch + 1) * 255 * 0.5  # [-1, 1] -> [0, 255]
    return batch


def load_vgg(gpu_ids):
    vgg = Vgg()
    vgg.cuda(device=gpu_ids[0])
    vgg.load_state_dict(torch.load('vgg16.weight'))  # Adding the weights to the model
    vgg = torch.nn.DataParallel(vgg, gpu_ids)
    return vgg
